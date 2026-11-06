"""
TallyWalls Desktop Application Launcher.
Starts the local enterprise reconciliation engine and opens the dashboard in the default browser.
Supports clean single-instance detection, dynamic port allocation, and headless background execution.
"""
import os
import sys
import time
import socket
import webbrowser
import threading

# Prevent Playwright / subprocess errors when running without a console window
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# Inject paths
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    MEIPASS = getattr(sys, "_MEIPASS", CURRENT_DIR)
    sys.path.insert(0, MEIPASS)
    sys.path.insert(0, os.path.join(MEIPASS, "app"))
else:
    sys.path.insert(0, CURRENT_DIR)
    sys.path.insert(0, os.path.join(CURRENT_DIR, "app"))

from app.main import app

def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0

def check_already_running(port: int = 8000) -> bool:
    """Check if TallyWalls is already running on this machine."""
    import urllib.request
    try:
        url = f"http://127.0.0.1:{port}/api/health"
        req = urllib.request.Request(url, headers={"User-Agent": "TallyWalls-Launcher"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass
    return False

def open_browser_when_ready(url: str, port: int):
    """Wait for FastAPI server to start accepting connections, then open browser."""
    for _ in range(60):
        if is_port_in_use(port):
            time.sleep(0.3)
            webbrowser.open(url)
            return
        time.sleep(0.1)
    webbrowser.open(url)

def main():
    target_port = 8000
    target_url = f"http://127.0.0.1:{target_port}/"

    # Single-instance handling: if already running, focus browser tab and exit cleanly
    if check_already_running(target_port):
        webbrowser.open(target_url)
        sys.exit(0)

    # If port 8000 is occupied by something else, find an available port
    if is_port_in_use(target_port):
        for p in range(8001, 8020):
            if not is_port_in_use(p):
                target_port = p
                target_url = f"http://127.0.0.1:{target_port}/"
                break

    # Launch browser opener in background thread
    threading.Thread(target=open_browser_when_ready, args=(target_url, target_port), daemon=True).start()

    # Run uvicorn server (blocking main thread)
    import uvicorn
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=target_port,
        log_level="error",
        access_log=False
    )

if __name__ == "__main__":
    main()
