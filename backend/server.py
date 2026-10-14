import argparse
import asyncio
import os
import uvicorn
from backend.app import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server=uvicorn.Server(uvicorn.Config(create_app(), host="127.0.0.1", port=args.port, log_level="warning", access_log=False))
    async def serve():
        async def watch_parent():
            if os.name!='nt' or not os.environ.get('AGENTOS_PARENT_PID'): return
            import ctypes
            from ctypes import wintypes
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
            kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            kernel.CloseHandle.argtypes=[wintypes.HANDLE]
            handle=kernel.OpenProcess(0x100000,False,int(os.environ['AGENTOS_PARENT_PID']))
            if not handle:
                server.should_exit=True
                return
            try:
                while not server.should_exit:
                    if kernel.WaitForSingleObject(handle,0)!=258:
                        server.should_exit=True
                        return
                    await asyncio.sleep(.5)
            finally: kernel.CloseHandle(handle)
        watcher=asyncio.create_task(watch_parent())
        try: await server.serve()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher,return_exceptions=True)
    asyncio.run(serve())


if __name__ == "__main__": main()
