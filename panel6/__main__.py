"""Запуск: python -m panel6 (или Panel6.exe). Флаг --selftest проверяет сборку и сразу выходит."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback


def setup_logging() -> str:
    from . import config

    path = str(config.app_dir() / "panel6.log")
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return path


def fix_console() -> None:
    """Консоль Windows бывает в cp1252, а в собранном .exe без консоли потоков нет вовсе."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        else:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    fix_console()
    selftest = "--selftest" in sys.argv
    unattended = selftest or "--screens" in sys.argv  # без человека: ошибки не показываем окном
    if selftest:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    log_path = setup_logging()
    log = logging.getLogger("panel6")

    from PySide6.QtWidgets import QApplication, QMessageBox

    from . import VERSION, config
    from .app import MainWindow, apply_theme
    from .controller import Controller

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    apply_theme(app)

    def excepthook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        log.error("необработанная ошибка:\n%s", text)
        if sys.stderr:
            sys.stderr.write(text)
        if unattended:
            os._exit(1)  # окно с ошибкой некому закрыть
        QMessageBox.critical(None, "Panel6", f"Ошибка: {value}\n\nПодробности в {log_path}")

    sys.excepthook = excepthook
    log.info("Panel6 %s, конфиг %s", VERSION, config.config_path())

    cfg = config.load()
    config.save(cfg)
    ctl = Controller(cfg)
    win = MainWindow(ctl)
    win.place_on_panel_monitor()
    if "--screens" in sys.argv:
        # снимки окон панели для проверки внешнего вида (используется в сборке на GitHub)
        from pathlib import Path

        from .screens import render_screens

        out = Path(sys.argv[sys.argv.index("--screens") + 1])
        render_screens(app, ctl, win, out)
        print(f"screens saved to {out}")
        win.quit()
        return 0
    if selftest:
        win.show()
        win.grid.open_grid()
        win.grid.close_grid()
        win.open_macros()
        win.macros_dialog.close()
        ctl.refresh()
        app.processEvents()
        print(f"selftest ok: Panel6 {VERSION}, горячих клавиш {len(ctl.bindings())}, ошибок {ctl.hotkey_errors}")
        win.quit()
        return 0
    ctl.start_hotkeys()
    ctl.start_netguard()
    win.show()
    ctl.refresh()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
