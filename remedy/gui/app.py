import sys
from pathlib import Path
import shutil
import json
import signal

from PyQt5.QtWidgets import QProgressDialog, QApplication
from PyQt5.QtCore import (
    QRunnable,
    pyqtSlot,
    pyqtSignal,
    QObject,
    QStandardPaths,
    QCoreApplication,
)

from remedy.remarkable.config import *
from remedy.gui.qmetadata import *
from remedy.remarkable.filesource import (
    LocalFileSource,
    LiveFileSourceSSH,
    LiveFileSourceRsync,
)
import remedy.gui.resources
from remedy.gui.notebookview import *
from remedy.gui.browser import *
from remedy.connect import connect, BadHostKeyException, UnknownHostKeyException

import time
from remedy.utils import log, logging


def _mkIcon(fn):
    icon = QPixmap(fn)
    icon.setDevicePixelRatio(QApplication.instance().devicePixelRatio())
    return icon


class RemedyApp(QApplication):
    _rootWindows = []  # this is a place to store top level windows
    # to avoid them being collected for going out of scope

    def __init__(self, args):
        QApplication.__init__(self, args)
        self.setQuitOnLastWindowClosed(False)

        self.setOrganizationDomain("emanueledosualdo.com")
        self.setApplicationName("remedy")
        self.setApplicationDisplayName("Remedy")
        self.setWindowIcon(QIcon(":/assets/remedy.svg"))
        self.setAttribute(Qt.AA_DontShowIconsInMenus, True)

        self._makeAppPaths()
        config = self.config = RemedyConfig(argv=sys.argv, paths=self.paths)

        log.setLevel(config.logLevel())

        log.info("Configuration loaded from %s.", config.path() or "defaults")
        log.debug("Cache at '%s'", self.paths.cache_dir)
        log.debug("Known hosts at '%s'", self.paths.known_hosts)

        self.aboutToQuit.connect(self.cleanup)
        self.fsource = None

    @pyqtSlot()
    def cleanup(self):
        log.info("Waiting for stray threads")
        QThreadPool.globalInstance().waitForDone()
        log.info("Done waiting")
        if self.fsource:
            self.fsource.cleanup()
            self.fsource.close()

    def sourceSelectionBox(self):
        sources = self.config.get("sources")
        return QInputDialog.getItem(
            None,
            "Source selection",
            "Source:",
            [s for s in sorted(sources) if not sources[s].get("hidden", False)],
            editable=False,
        )

    def _makeAppPaths(self):
        conf_dir = Path(
            QStandardPaths.standardLocations(QStandardPaths.ConfigLocation)[0]
        )
        old = conf_dir / "remedy.json"
        conf_dir = conf_dir / "remedy"
        conf_file = conf_dir / "config.json"
        conf_dir.mkdir(parents=True, exist_ok=True)
        if old.is_file():  # migrate
            log.warning("Old configuration file '%s' moved to '%s'.", old, conf_file)
            old.rename(conf_file)
        try:
            cache_dir = Path(
                QStandardPaths.standardLocations(QStandardPaths.CacheLocation)[0]
            )
        except Exception:
            cache_dir = None

        self._paths = AppPaths(conf_dir, conf_file, conf_dir / "known_hosts", cache_dir)

    @property
    def paths(self):
        return self._paths

    _init = None
    initDialog = None

    def requestInit(self, **overrides):
        self.fsource = None
        self.setQuitOnLastWindowClosed(False)

        sources = self.config.get("sources")
        source = self.config.selectedSource()

        if len(sources) == 0:
            mbox = QMessageBox(
                QMessageBox.Warning,
                "Configuration error",
                "No sources defined in current configuration.",
            )
            mbox.setInformativeText(
                "<big>Please locate or create the file"
                "<p><code>%s</code></p>"
                "and add configurations to it according"
                "to the <a href='https://github.com/michaelmera/remedy/#configuration'>documentation</a>.</big>"
                % self._paths.config
            )
            confBtn = mbox.addButton("Open Config…", QMessageBox.HelpRole)
            mbox.addButton(QMessageBox.Close)
            mbox.exec()
            if mbox.clickedButton() == confBtn:
                self.openSettings(prompt=False)
            return False

        if source is None:
            source = self.config.get("default_source")
            if not source:
                source, ok = self.sourceSelectionBox()
                if not ok:
                    log.error("Sorry, I need a source to work.")
                    return False
            self.config.selectSource(source)

        self.initDialog = RemedyProgressDialog(label="Loading: ")
        init = RemedyInitWorker(*self.config.connectionArgs(**overrides))
        self.initDialog.canceled.connect(init.signals.cancelInit)
        init.signals.success.connect(self.initialised)
        init.signals.error.connect(self.retryInit)
        init.signals.canceled.connect(self.canceledInit)
        init.signals.progress.connect(self.initDialog.onProgress)
        QThreadPool.globalInstance().start(init)
        return True

    @pyqtSlot(Exception)
    def retryInit(self, e):
        log.error("RETRY? [%s]", e)
        mbox = QMessageBox(
            QMessageBox.NoIcon, "Connection error", "Connection attempt failed"
        )
        mbox.addButton("Settings…", QMessageBox.ResetRole)
        mbox.addButton(QMessageBox.Cancel)
        if isinstance(e, EOFError):
            mbox.setIconPixmap(_mkIcon(":/assets/128/security-low.svg"))
            mbox.setDetailedText(str(e))
            mbox.setInformativeText(
                "<big>The host at %s abruptly refused connection.<br>"
                "This may happen just after a software update on the tablet.<br>"
                "It may be possible to fix this by resetting the known hosts keys.</big><br><br>"
                "You have two options to do this:"
                "<ol><li>"
                "Press 'Reset key' to reset Remedy's internal keys."
                "<br></li><li>"
                "Change your <code>~/.ssh/known_hosts</code> file to match the new fingerprint.<br>"
                "The easiest way to do this is connecting manually via ssh and follow the instructions."
                "<br></li></ol>" % (e.hostname)
            )
            mbox.addButton("Ignore", QMessageBox.NoRole)
            mbox.addButton("Reset keys", QMessageBox.YesRole)
        elif isinstance(e, BadHostKeyException):
            mbox.setIconPixmap(_mkIcon(":/assets/128/security-low.svg"))
            mbox.setDetailedText(str(e))
            mbox.setInformativeText(
                "<big>The host at %s has the wrong key.<br>"
                "This usually happens just after a software update on the tablet.</big><br><br>"
                "You have three options to fix this permanently:"
                "<ol><li>"
                "Press Update to replace the old key with the new."
                "<br></li><li>"
                "Change your <code>~/.ssh/known_hosts</code> file to match the new fingerprint.<br>"
                "The easiest way to do this is connecting manually via ssh and follow the instructions."
                "<br></li><li>"
                'Set <code>"host_key_policy": "ignore_new"</code> in the appropriate source of Remedy\'s settings.<br>'
                "This is not recommended unless you are in a trusted network."
                "<br></li></ol>" % (e.hostname)
            )
            mbox.addButton("Ignore", QMessageBox.NoRole)
            mbox.addButton("Update", QMessageBox.YesRole)
        elif isinstance(e, UnknownHostKeyException):
            mbox.setIconPixmap(_mkIcon(":/assets/128/security-high.svg"))
            mbox.setDetailedText(str(e))
            mbox.setInformativeText(
                "<big>The host at %s is unknown.<br>"
                "This usually happens if this is the first time you use ssh with your tablet.</big><br><br>"
                "You have three options to fix this permanently:"
                "<ol><li>"
                "Press Add to add the key to the known hosts."
                "<br></li><li>"
                "Change your <code>~/.ssh/known_hosts</code> file to match the new fingerprint.<br>"
                "The easiest way to do this is connecting manually via ssh and follow the instructions."
                "<br></li><li>"
                'Set <code>"host_key_policy": "ignore_new"</code> in the appropriate source of Remedy\'s settings.<br>'
                "This is not recommended unless you are in a trusted network."
                "<br></li></ol>" % (e.hostname)
            )
            mbox.addButton("Ignore", QMessageBox.NoRole)
            mbox.addButton("Add", QMessageBox.YesRole)
        else:
            mbox.setIconPixmap(_mkIcon(":/assets/dead.svg"))
            mbox.setInformativeText(
                "I could not connect to the reMarkable at %s:\n%s."
                % (self.config.get("host", "[no source selected]"), e)
            )
            d = mbox.addButton(QMessageBox.Discard)
            d.setText("Source…")
            mbox.addButton(QMessageBox.Retry)
            mbox.setDefaultButton(QMessageBox.Retry)
        answer = mbox.exec()
        if answer == QMessageBox.Retry:
            self.requestInit()
        elif answer == QMessageBox.Cancel:
            self.quit()
        elif answer == QMessageBox.Discard:  # Sources selection
            source, ok = self.sourceSelectionBox()
            if not ok:
                self.quit()
            else:
                self.config.selectSource(source)
                self.requestInit()
        elif answer == 1:  # Ignore
            self.requestInit(host_key_policy="ignore_all")
        elif answer == 2:  # Add/Update
            local_kh = self.paths.known_hosts
            if not local_kh.is_file():
                open(local_kh, "a").close()
            from paramiko import HostKeys

            hk = HostKeys(local_kh)
            if hasattr(e, "key"):
                hk.add(e.hostname, e.key.get_name(), e.key)
            else:
                hk.clear()
            hk.save(local_kh)
            log.info("Saved host key in %s", local_kh)
            self.requestInit()
        else:
            self.openSettings(prompt=False)
            self.quit()

    @pyqtSlot()
    def canceledInit(
        self,
    ):
        log.fatal("Canceled")
        self.retryInit(RemedyInitCancel("Canceled initialisation"))

    @pyqtSlot(RemarkableIndex)
    def initialised(self, index):
        self._init = None
        self.initDialog = None
        self.tree = FileBrowser(index)
        self.fsource = index.fsource  # for cleanup
        self.setQuitOnLastWindowClosed(True)
        log.info("Initialised, launching browser")

    @pyqtSlot()
    def openSettings(self, prompt=True):
        if self.paths.config is None:
            QMessageBox.critical("Configuration", "No configuration path found")
            self.quit()
            return
        log.info("Configuration at '%s'", self.paths.config)
        if prompt:
            ans = QMessageBox.information(
                None,
                "Opening Settings",
                "To load the new settings you need to relaunch Remedy.",
                buttons=(QMessageBox.Ok | QMessageBox.Cancel),
                defaultButton=QMessageBox.Ok,
            )
            if ans == QMessageBox.Cancel:
                return

        confpath = self.paths.config
        if not confpath.is_file():
            confpath = confpath.resolve()
            confpath.parent.mkdir(exist_ok=True)
            with open(confpath, "w") as f:
                self.config.dump(f)
        QDesktopServices.openUrl(QUrl(confpath.as_uri()))
        self.quit()


class RemedyProgressDialog(QProgressDialog):
    def __init__(self, title="", label="", parent=None):
        QProgressDialog.__init__(self, parent)
        self.label = label
        self.setWindowTitle(title)
        self.setMinimumWidth(300)
        self.setLabelText(label)
        self.setMinimumDuration(500)
        self.setAutoClose(True)

    @pyqtSlot(int, int, str)
    def onProgress(self, x, tot, txt):
        self.setMaximum(tot)
        self.setValue(x)
        lbl = self.label + txt
        if len(lbl) > 35:
            lbl = lbl[:35] + "…"
        self.setLabelText(lbl)

    @pyqtSlot()
    @pyqtSlot(Exception)
    def calledOff(self, e=None):
        self.close()


class RemedyInitCancel(Exception):
    pass


class RemedyInitSignals(QObject):
    success = pyqtSignal(RemarkableIndex)
    error = pyqtSignal(Exception)
    canceled = pyqtSignal()
    progress = pyqtSignal(int, int, str)
    _cancel = False

    @pyqtSlot()
    def cancelInit(self):
        log.info("Cancel initialisation requested")
        self._cancel = True


class RemedyInitWorker(QRunnable):
    _cancel = False

    def __init__(self, stype, args):
        QRunnable.__init__(self)
        self.signals = RemedyInitSignals()
        self.stype = stype
        self.args = args

    # @pyqtSlot(int,int,str)
    def _progress(self, x, tot, txt="Initialising"):
        if self.signals._cancel:
            self.signals.progress.emit(1, 1, "Error")
            # self.signals.progress.disconnect()
            raise RemedyInitCancel("Canceled initialisation")
        self.signals.progress.emit(x, tot, txt)

    def run(self):
        args = self.args

        # host should be assumed to be the address (unless specified otherwise)
        if "host" in args and not "address" in args:
            args["address"] = args["host"]

        app = QApplication.instance()
        fsource = None
        try:
            if self.stype == "local":
                self._progress(0, 0, "Initialising...")
                fsource = LocalFileSource(
                    args.get("name"), args.get("documents"), args.get("templates")
                )
            else:
                self._progress(0, 0, "Connecting...")
                ssh = connect(**args)
                if self.stype == "ssh":
                    if app.paths.cache_dir is None:
                        self.signals.error.emit(
                            Exception("Error locating the cache folder")
                        )
                        return
                    fsource = LiveFileSourceSSH(ssh, **args)
                elif self.stype == "rsync":
                    fsource = LiveFileSourceRsync(ssh, **args)

            if fsource is None:
                self.signals.error.emit(
                    Exception("Could not find the reMarkable data!")
                )
                return

            T0 = time.perf_counter()
            self._progress(0, 0, "Fetching metadata")
            fsource.prefetchMetadata(progress=self._progress)
            self._progress(0, 0, "Building index")
            index = QRemarkableIndex(fsource, progress=self._progress)
            self._progress(4, 4, "Done")
            log.info("LOAD TIME: %f", time.perf_counter() - T0)
            self.signals.success.emit(index)
        except RemedyInitCancel:
            if fsource:
                fsource.cleanup()
                fsource.close()
            self.signals.canceled.emit()
        except Exception as e:
            self.signals.error.emit(e)


def main():
    QCoreApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
    log.setLevel(logging.INFO)
    log.info("STARTING: %s", time.asctime())
    try:
        app = RemedyApp(sys.argv)
    except RemedyConfigException as e:
        log.fatal("Misconfiguration: %s", str(e))
        sys.exit(1)

    if app.requestInit():
        signal.signal(signal.SIGINT, lambda *args: app.quit())
        ecode = app.exec_()
        log.info("QUITTING: %s", time.asctime())
        sys.exit(ecode)
    else:
        log.info("Could not start the app, quitting.")


# THE APP IS EXITING BECAUSE IT NEEDS OPEN DIALOGS TO STAY ALIVE
# Either handle autoclosing manually
# or keep some dialog always open...

if __name__ == "__main__":
    main()
