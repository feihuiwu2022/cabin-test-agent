"""PyInstaller 入口：cabin-agent.exe。"""
import multiprocessing

from cabin_agent.daemon import run

if __name__ == "__main__":
    multiprocessing.freeze_support()
    run()
