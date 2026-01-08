import os
import sys
import threading
import platform
from pathlib import Path
import sip

from qgis.PyQt import uic
from qgis.PyQt import QtCore, QtWidgets
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapLayer,
    QgsProject,
    QgsVectorLayer,
    QgsWkbTypes,
)

# This loads your .ui file so that PyQt can populate your plugin with the elements from Qt Designer
FORM_CLASS, _ = uic.loadUiType(os.path.join(
    os.path.dirname(__file__), 'gedi_pipeline_plugin_dialog_base.ui'))


class StreamToSignal:
    """Redirect text writes to a Qt signal."""

    def __init__(self, signal):
        self.signal = signal

    def write(self, text):
        text = text.strip()
        if text:
            self.signal.emit(text)

    def flush(self):
        pass


class PipelineWorker(QtCore.QObject):
    log = QtCore.pyqtSignal(str)
    finished = QtCore.pyqtSignal(bool, list, str)

    def __init__(self, params, plugin_dir, cancel_event):
        super().__init__()
        self.params = params
        self.plugin_dir = plugin_dir
        self.cancel_event = cancel_event

    @QtCore.pyqtSlot()
    def run(self):
        stdout_orig, stderr_orig = sys.stdout, sys.stderr
        sys.stdout = StreamToSignal(self.log)
        sys.stderr = StreamToSignal(self.log)
        try:
            roi = self._compute_roi()
            self._prepare_netrc()
            pipeline = self._build_pipeline(roi)
            self.log.emit("[Pipeline] Starting pipeline...")
            granules = pipeline.run_pipeline()
            outputs = self._collect_outputs(granules)
            self.finished.emit(True, outputs, "")
        except Exception as e:
            self.finished.emit(False, [], str(e))
        finally:
            sys.stdout = stdout_orig
            sys.stderr = stderr_orig

    def _build_pipeline(self, roi):
        candidates = [
            os.path.join(self.plugin_dir, "GEDI-Pipeline"),
            os.path.join(self.plugin_dir, "pipeline"),
        ]
        framework_path = None
        for cand in candidates:
            if os.path.isdir(cand):
                framework_path = cand
                break
        if not framework_path:
            raise RuntimeError("Could not locate GEDI framework folder (expected 'GEDI-Pipeline' or 'pipeline').")
        
        if framework_path not in sys.path:
            sys.path.insert(0, framework_path)

        from .pipeline.pipeline.pipeline import GEDIPipeline

        beams = self.params["beams"] or None
        sds = self.params["sds"] or None

        return GEDIPipeline(
            out_directory=self.params["output_dir"],
            product=self.params["product"],
            version=self.params["version"],
            date_start=self.params["start_date"],
            date_end=self.params["end_date"],
            recurring_months=self.params["recurring_months"],
            roi=roi,
            beams=beams,
            sds=sds,
            persist_login=self.params["keep_login"],
            keep_original_file=self.params["keep_original"],
            cancel_event=self.cancel_event,
            roi_path=self.params.get("polygon_source") or None,
        )

    def _prepare_netrc(self):
        user = self.params["earthdata_user"]
        pwd = self.params["earthdata_pass"]
        if not user or not pwd:
            return
        
        content = f"machine urs.earthdata.nasa.gov login {user} password {pwd}\n"
        # Write both Unix and Windows-friendly filenames to avoid prompts in earthaccess
        netrc_files = [Path.home() / ".netrc"]
        if os.name == "nt":
            netrc_files.append(Path.home() / "_netrc")
        for netrc_path in netrc_files:
            try:
                netrc_path.write_text(content)
                try:
                    netrc_path.chmod(0o600)
                except Exception:
                    pass
                self.log.emit(f"[Auth] Wrote credentials to {netrc_path} for EarthData.")
            except Exception as e:
                self.log.emit(f"[Auth] Failed to write credentials to {netrc_path}: {e}")

    def _compute_roi(self):
        layer_id = self.params["polygon_layer_id"]
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if not layer:
            raise RuntimeError("No polygon layer selected.")

        if self.params["selected_features_only"] and layer.selectedFeatureCount() > 0:
            extent = layer.boundingBoxOfSelected()
            if not extent.isFinite():
                raise RuntimeError("Selected features have no valid extent.")
        else:
            extent = layer.extent()

        if not extent.isFinite():
            raise RuntimeError("Layer extent is not valid.")

        crs_src = layer.crs()
        crs_dest = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs_src != crs_dest:
            transform = QgsCoordinateTransform(crs_src, crs_dest, QgsProject.instance())
            extent = transform.transformBoundingBox(extent)

        # ROI format: [UL_LAT, UL_LON, LR_LAT, LR_LON]
        return [extent.yMaximum(), extent.xMinimum(), extent.yMinimum(), extent.xMaximum()]

    def _collect_outputs(self, granules):
        outputs = []
        out_dir = Path(self.params["output_dir"])
        return [str(p) for p in out_dir.glob("*.gpkg")]


class GEDIPipelineDialog(QtWidgets.QDialog, FORM_CLASS):
    def __init__(self, parent=None, plugin_dir=None):
        """Constructor."""
        super(GEDIPipelineDialog, self).__init__(parent)
        self.setupUi(self)
        self.plugin_dir = plugin_dir or os.path.dirname(__file__)
        self._worker_thread = None
        self._cancel_event = threading.Event()

        self._init_polygon_menu()
        self.populate_polygon_layers()

        self.run_pipeline.clicked.connect(self.on_run_clicked)
        self.close_button.clicked.connect(self.on_cancel_close)
        self.browse_output_btn.clicked.connect(self.choose_output_dir)
        self.polygon_layer_combo.currentIndexChanged.connect(self.on_polygon_layer_changed)

    def _init_polygon_menu(self):
        menu = QtWidgets.QMenu(self)
        from_file = menu.addAction("From computer")
        browse_layer = menu.addAction("Browse layer")
        refresh_layers = menu.addAction("Refresh layers")

        from_file.triggered.connect(self.on_polygon_from_file)
        browse_layer.triggered.connect(self.on_polygon_browse_layer)
        refresh_layers.triggered.connect(self.populate_polygon_layers)

        self.polygon_options_btn.setMenu(menu)

    def choose_output_dir(self):
        directory = QtWidgets.QFileDialog.getExistingDirectory(
            self,
            "Select output folder",
            os.path.expanduser("~")
        )
        if directory:
            self.output_dir_lineedit.setText(directory)

    def populate_polygon_layers(self):
        """Fill combo with polygon layers currently loaded in QGIS."""
        current_id = self.polygon_layer_combo.currentData()
        self.polygon_layer_combo.blockSignals(True)
        self.polygon_layer_combo.clear()

        polygon_layers = []
        for layer in QgsProject.instance().mapLayers().values():
            if layer.type() != QgsMapLayer.VectorLayer:
                continue
            if QgsWkbTypes.geometryType(layer.wkbType()) == QgsWkbTypes.PolygonGeometry:
                polygon_layers.append(layer)

        if not polygon_layers:
            self.polygon_layer_combo.addItem("No polygon layers found", None)
            self.polygon_layer_combo.setEnabled(False)
        else:
            self.polygon_layer_combo.setEnabled(True)
            for layer in polygon_layers:
                self.polygon_layer_combo.addItem(layer.name(), layer.id())
            if current_id:
                idx = self.polygon_layer_combo.findData(current_id)
                if idx != -1:
                    self.polygon_layer_combo.setCurrentIndex(idx)

        self.polygon_layer_combo.blockSignals(False)
        self.on_polygon_layer_changed()

    def on_polygon_layer_changed(self):
        layer_id = self.polygon_layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if layer:
            self.polygon_path_lineedit.setText(layer.source())
        else:
            self.polygon_path_lineedit.clear()

    def on_polygon_from_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select polygon file",
            os.path.expanduser("~"),
            "Vector files (*.shp *.gpkg);;All files (*)"
        )
        if not path:
            return

        layer_name = os.path.splitext(os.path.basename(path))[0]
        layer = QgsVectorLayer(path, layer_name, "ogr")
        if not layer.isValid():
            QtWidgets.QMessageBox.critical(self, "Invalid layer", "Could not load the selected polygon file.")
            return

        QgsProject.instance().addMapLayer(layer)
        self.populate_polygon_layers()
        idx = self.polygon_layer_combo.findData(layer.id())
        if idx != -1:
            self.polygon_layer_combo.setCurrentIndex(idx)
        self.polygon_path_lineedit.setText(path)

    def on_polygon_browse_layer(self):
        """Refresh layers and open the dropdown to prompt selection."""
        self.populate_polygon_layers()
        self.polygon_layer_combo.showPopup()

    def on_run_clicked(self):
        params = self.collect_parameters()
        if not params["output_dir"]:
            QtWidgets.QMessageBox.warning(self, "Missing output", "Please choose an output folder.")
            return
        if not params["polygon_layer_id"] and not params["polygon_source"]:
            QtWidgets.QMessageBox.warning(self, "Missing AOI", "Please select a polygon layer or load a polygon file.")
            return
        if not self._has_credentials(params):
            QtWidgets.QMessageBox.warning(
                self,
                "Missing credentials",
                "Please enter your EarthData username and password (or ensure a .netrc/_netrc file exists).",
            )
            return
        if not self.check_dependencies():
            return
        if self._worker_thread and self._worker_thread.isRunning():
            QtWidgets.QMessageBox.information(self, "Pipeline running", "Please wait for the current run to finish.")
            return

        self.log_text_edit.clear()
        self.progress_bar.setRange(0, 0)  # busy indicator
        self._cancel_event.clear()
        self.close_button.setText("Stop")
        self._start_worker(params)

    def collect_parameters(self):
        """Capture current UI values into a dictionary."""
        start_date = self.start_date_edit.date().toString("yyyy.MM.dd")
        end_date = self.end_date_edit.date().toString("yyyy.MM.dd")
        return {
            "output_dir": self.output_dir_lineedit.text().strip(),
            "product": self.product_combo.currentText(),
            "version": self.version_combo.currentText(),
            "start_date": start_date,
            "end_date": end_date,
            "recurring_months": self.recurring_months_check.isChecked(),
            "polygon_layer_id": self.polygon_layer_combo.currentData(),
            "polygon_source": self.polygon_path_lineedit.text().strip(),
            "selected_features_only": self.selected_only_check.isChecked(),
            "earthdata_user": self.earthdata_user_edit.text().strip(),
            "earthdata_pass": self.earthdata_pass_edit.text(),
            "keep_login": self.keep_login_check.isChecked(),
            "beams": self.beams_lineedit.text().strip(),
            "sds": self.sds_lineedit.text().strip(),
            "keep_original": self.keep_original_check.isChecked(),
        }
    
    def _has_credentials(self, params):
        # Allow explicit credentials or existing netrc/_netrc files
        if params["earthdata_user"] and params["earthdata_pass"]:
            return True
        home = Path.home()
        for candidate in [home / ".netrc", home / "_netrc"]:
            if candidate.exists() and candidate.stat().st_size > 0:
                return True
        return False
    
    def check_dependencies(self):
        """Ensure required Python deps are available in the QGIS environment."""
        missing = []
        hdf5_mismatch = None
        # h5py special handling
        try:
            import h5py

            built = getattr(h5py.version, "hdf5_built_version", None)
            runtime = getattr(h5py.version, "hdf5_version", None)
            if built is None and hasattr(h5py.version, "hdf5_built_version_tuple"):
                built = ".".join(map(str, h5py.version.hdf5_built_version_tuple))
            if runtime is None and hasattr(h5py.version, "hdf5_version_tuple"):
                runtime = ".".join(map(str, h5py.version.hdf5_version_tuple))
            if built and runtime and built.split(".")[:2] != runtime.split(".")[:2]:
                hdf5_mismatch = (built, runtime)
        except ImportError:
            missing.append("h5py")
        for mod in ["pandas", "geopandas", "numpy", "shapely", "earthaccess", "requests"]:
            try:
                __import__(mod)
            except ImportError:
                missing.append(mod)

        if not missing and not hdf5_mismatch:
            return True

        msg_lines = []
        if missing:
            msg_lines.append("Missing Python packages:\n  " + ", ".join(sorted(set(missing))))

        py_path = sys.executable
        os_name = platform.system()
        if hdf5_mismatch:
            built, runtime = hdf5_mismatch
            msg_lines.append(f"h5py/HDF5 version mismatch (built against {built}, running with {runtime}).")

        if os_name == "Windows":
            msg_lines.append(
                "Windows/OSGeo4W: run the OSGeo4W installer in Advanced mode and add package 'python3-h5py' "
                "and other missing ones, or in OSGeo4W Shell run:\n"
                f'  python -m pip install --user ' + " ".join(sorted(set(missing))) if missing else ""
            )
        elif os_name == "Darwin":
            msg_lines.append(
                "macOS: use QGIS Python to install:\n"
                f'  python -m pip install --user ' + " ".join(sorted(set(missing))) if missing else ""
            )
        else:
            msg_lines.append(
                "Linux (Debian/Ubuntu):\n"
                "  sudo apt install python3-h5py python3-pandas python3-geopandas python3-shapely"
            )

        QtWidgets.QMessageBox.critical(self, "Missing dependencies", "\n\n".join([m for m in msg_lines if m]))
        return False

    def _start_worker(self, params):
        self._worker_thread = QtCore.QThread(self)
        self._worker = PipelineWorker(params, self.plugin_dir, self._cancel_event)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.log.connect(self.append_log)
        self._worker.finished.connect(self.on_worker_finished)
        self._worker.finished.connect(self._worker_thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker_thread.finished.connect(self._worker_thread.deleteLater)
        self._worker_thread.start()

    @QtCore.pyqtSlot(str)
    def append_log(self, message):
        self.log_text_edit.append(message)

    @QtCore.pyqtSlot(bool, list, str)
    def on_worker_finished(self, success, outputs, error_message):
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1 if success else 0)
        if success:
            self.log_text_edit.append("[Pipeline] Finished.")
            self._load_outputs(outputs)
        else:
            self.log_text_edit.append(f"[Error] {error_message}")

        self._worker_thread = None
        self.close_button.setText("Close")

    def _load_outputs(self, outputs):
        if not outputs:
            self.log_text_edit.append("[Loader] No subset files were produced.")
            return
        added = 0
        for path in outputs:
            layer = QgsVectorLayer(path, os.path.basename(path), "ogr")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                added += 1
                self.log_text_edit.append(f"[Loader] Added {path}")
            else:
                self.log_text_edit.append(f"[Loader] Failed to load {path}")
        if added:
            self.log_text_edit.append(f"[Loader] Loaded {added} layer(s) into QGIS.")

    def on_cancel_close(self):
        try:
            if (
                self._worker_thread
                and not sip.isdeleted(self._worker_thread)
                and self._worker_thread.isRunning()
            ):
                self._cancel_event.set()
                self.log_text_edit.append("[Pipeline] Cancellation requested. Waiting for current task to stop...")
                self.close_button.setText("Stop")
                return
        except RuntimeError:
            # thread wrapper already deleted; fall through to close
            pass
        self.reject()
