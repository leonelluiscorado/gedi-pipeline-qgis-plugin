from .finder import *
from .subsetter import *
from .downloader import *
import geopandas as gp

"""
Script that controls the entire GEDI Finder - Downloader - Subsetter pipeline.
"""

class GEDIPipeline:
    """
    The GEDIPipeline :class: performs all operations in selecting, downloading and subsetting GEDI Data for a given region of interest
    Args:
        out
    """

    def __init__(self, out_directory, product, version, date_start, date_end, roi, sds, beams, recurring_months=False, persist_login=False, keep_original_file=False, cancel_event=None, roi_path=None):

        self.product = product
        self.version = version
        self.date_start, self.date_end = date_start, date_end
        self.recurring_months = recurring_months
        self.keep_original_file = keep_original_file

        self.roi_path = roi_path

        if isinstance(roi, list):
            self.roi = [float(c) for c in roi]

        if isinstance(roi, str):
            self.roi = [float(c) for c in roi.split(",")]

        self.roi_gdf = None
        if self.roi_path:
            try:
                gdf = gp.read_file(self.roi_path)
                if gdf.crs is None:
                    print("[Pipeline] ROI file has no CRS. Assuming EPSG:4326.")
                    gdf.set_crs(epsg=4326, inplace=True)
                else:
                    gdf = gdf.to_crs(epsg=4326)
                self.roi_gdf = gdf
                minx, miny, maxx, maxy = gdf.total_bounds
                self.roi = [maxy, minx, miny, maxx]  # UL_lat, UL_lon, LR_lat, LR_lon
                print(f"[Pipeline] ROI bounds derived from polygon: {self.roi}")
            except Exception as e:
                print(f"[Pipeline] Failed to read ROI polygon file: {e}")

        self.out_directory = out_directory
        self.sds = sds
        self.beams = beams
        self.persist_login = persist_login
        self.cancel_event = cancel_event

        self.finder = GEDIFinder(
            product=self.product,
            version=self.version,
            date_start=self.date_start,
            date_end=self.date_end,
            recurring_months=self.recurring_months,
            roi=self.roi
        )
        
        self.downloader = GEDIDownloader(
            persist_login=self.persist_login,
            save_path=self.out_directory
        )

        self.subsetter = GEDISubsetter(
            roi=self.roi,
            product=self.product,
            out_dir=self.out_directory,
            sds=self.sds,
            beams=self.beams,
            roi_gdf=self.roi_gdf
        )

        # Make dir if not exists
        if not os.path.exists(out_directory):
            os.mkdir(out_directory)


    def run_pipeline(self):

        all_granules = self.finder.find(output_filepath=self.out_directory, save_file=True)

        # Start download for every granule
        for g in all_granules:
            if self.cancel_event is not None and self.cancel_event.is_set():
                print("[Pipeline] Cancelled by user.")
                break

            if os.path.exists(os.path.join(self.out_directory, g[0].split("/")[-1].replace(".h5", ".gpkg"))):
                print(f"Skipping granule from link {g} as it is already subsetted.")
                continue

            # Try Download
            if not self.downloader.download_granule(g[0]):
                retries = 3
                print(f"[Downloader] Fail download for link {g}. Retrying...")
                for r in retries:
                    print(f"Retry {r}")
                    if not self.downloader.download_granule(g[0]):
                        retries -= 1
                        continue
                print(f"[Downloader] Fail download for link {g}. Skipping...")
                continue

            # Subset
            self.subsetter.subset(os.path.join(self.out_directory, g[0].split("/")[-1]))

            # Delete original file and keep subset to ROI granule to save space
            if not self.keep_original_file:
                os.remove(os.path.join(self.out_directory, g[0].split("/")[-1]))

        return all_granules
