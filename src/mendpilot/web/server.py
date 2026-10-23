"""Loopback-only FastAPI service for repairing one uploaded Python file."""
import argparse
from contextlib import asynccontextmanager
import hmac
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from mendpilot.runner import validate_config
from mendpilot.web.tasks import BusyError, MAX_FILE_BYTES, TERMINAL, TaskManager


STATIC = Path(__file__).with_name('static')


def create_app(output_dir=None):
    manager = TaskManager(output_dir or Path.cwd() / 'artifacts/web')
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        # Cancelling may wait for the child to save its output.
        import asyncio
        await asyncio.to_thread(manager.close)

    app = FastAPI(title='MendPilot', docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.manager = manager

    @app.middleware('http')
    async def local_requests_only(request, call_next):
        host = request.headers.get('host', '')
        try:
            hostname = urlsplit('http://' + host).hostname
        except ValueError:
            return JSONResponse({'detail': '无效的 Host。'}, status_code=400)
        if hostname not in {'localhost', '127.0.0.1', '::1'}:
            return JSONResponse({'detail': '仅允许本机访问；远程使用请建立 SSH 隧道。'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != 'http://' + host:
            return JSONResponse({'detail': '不允许跨站请求。'}, status_code=403)
        if request.method not in {'GET', 'HEAD'}:
            if not hmac.compare_digest(request.headers.get('x-repair-token', '').encode('utf-8'), token.encode('ascii')):
                return JSONResponse({'detail': '页面会话已失效，请刷新页面。'}, status_code=403)
            try:
                size = int(request.headers.get('content-length', '0'))
            except ValueError:
                return JSONResponse({'detail': '无效的请求大小。'}, status_code=400)
            if size < 0 or size > MAX_FILE_BYTES + 128 * 1024:
                return JSONResponse({'detail': '上传请求过大。'}, status_code=413)
            if request.url.path == '/api/tasks' and not size:
                return JSONResponse({'detail': '上传请求需要 Content-Length。'}, status_code=411)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        return response

    @app.get('/', response_class=HTMLResponse)
    def index():
        return STATIC.joinpath('index.html').read_text(encoding='utf-8').replace('__SESSION_TOKEN__', token)

    @app.get('/api/defaults')
    def defaults():
        return dict(model='qwen2.5-7b-instruct', base_url='http://127.0.0.1:8002/v1',
                    api='chat_completions', python=sys.executable, supported=os.name == 'posix')

    @app.post('/api/tasks', status_code=202)
    async def submit(request: Request, file: UploadFile = File(...), task: str = Form(...),
                     model: str = Form(...), base_url: str = Form(...),
                     api: str = Form('chat_completions'), api_key: str = Form('')):
        try:
            if os.name != 'posix':
                raise ValueError('本地修复需要 Linux/macOS 的 bash 和 git；Windows 请使用 WSL 或连接 Linux 服务器。')
            if len((await request.form()).getlist('file')) != 1:
                raise ValueError('每次只能上传一个文件。')
            if not task.strip() or len(task) > 20000:
                raise ValueError('请填写问题描述，长度不能超过 20,000 字符。')
            if len(model) > 200 or len(base_url) > 2000 or len(api_key) > 4096:
                raise ValueError('模型连接信息过长。')
            config = validate_config({'model': dict(name=model.strip(), base_url=base_url.strip(), api=api)})
            if not api_key.strip() and urlsplit(base_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
                raise ValueError('远端模型服务需要填写 API key。')
            content = await file.read(MAX_FILE_BYTES + 1)
            task_id = manager.submit(file.filename, content, task.strip(), config, api_key.strip())
            return {'id': task_id}
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        except BusyError as error:
            raise HTTPException(409, str(error)) from None
        finally:
            await file.close()

    def find(task_id):
        try:
            return manager.get(task_id)
        except KeyError:
            raise HTTPException(404, '任务不存在；重启服务后请到输出目录查看历史结果。') from None

    @app.get('/api/tasks/{task_id}')
    def status(task_id: str):
        find(task_id)
        return manager.snapshot(task_id)

    @app.post('/api/tasks/{task_id}/cancel')
    def cancel(task_id: str):
        find(task_id)
        manager.cancel(task_id)
        return manager.snapshot(task_id)

    def artifact(task_id, kind):
        task = find(task_id)
        if task.status not in TERMINAL:
            raise HTTPException(409, '任务还未结束。')
        root = task.root / 'result'
        paths = {'file': root / 'fixed' / task.filename, 'patch': root / 'submitted.patch'}
        if kind not in paths:
            raise HTTPException(400, '仅支持下载修复文件或提交补丁。')
        path = paths[kind]
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise HTTPException(404, '本次没有生成该结果。')
        return path

    @app.get('/api/tasks/{task_id}/preview')
    def preview(task_id: str):
        patch = artifact(task_id, 'patch')
        with patch.open('r', encoding='utf-8', errors='replace') as stream:
            text = stream.read(128 * 1024 + 1)
        return {'patch': text[:128 * 1024], 'truncated': len(text) > 128 * 1024}

    @app.get('/api/tasks/{task_id}/download')
    def download(task_id: str, kind: str = 'file'):
        path = artifact(task_id, kind)
        return FileResponse(path, media_type='application/octet-stream', filename=path.name)

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--output-dir', type=Path, default=Path.cwd() / 'artifacts/web')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    if os.name != 'posix':
        parser.error('Local repair requires Linux/macOS with bash and git; use WSL or a Linux host.')
    import uvicorn
    print(f'代码修复网页：http://127.0.0.1:{args.port}', flush=True)
    print('运行环境：' + sys.executable, flush=True)
    uvicorn.run(create_app(args.output_dir), host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
