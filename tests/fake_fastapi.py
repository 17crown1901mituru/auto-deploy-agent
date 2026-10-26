"""テスト専用: FastAPI/pydantic が入っていない環境で main.py の中身(ルート関数)を直接呼ぶための最小の代用品。
本物の挙動(検証・シリアライズ・依存性注入)は再現しないので、ルート関数のロジックの確認だけに使う。"""
import sys
import types


class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _FieldInfo:
    def __init__(self, default=None, default_factory=None):
        self.default, self.default_factory = default, default_factory


def Field(default=None, default_factory=None):
    return _FieldInfo(default, default_factory)


class BaseModel:
    def __init__(self, **kw):
        for name in getattr(type(self), "__annotations__", {}):
            if name in kw:
                value = kw[name]
            else:
                default = getattr(type(self), name, None)
                value = default.default_factory() if isinstance(default, _FieldInfo) and default.default_factory else \
                    (default.default if isinstance(default, _FieldInfo) else default)
            setattr(self, name, value)

    def model_dump(self):
        return {k: (v.model_dump() if isinstance(v, BaseModel) else v) for k, v in self.__dict__.items()}


class FastAPI:
    def __init__(self, **kw):
        pass

    def add_middleware(self, *a, **k):
        pass

    def _deco(self, *a, **k):
        return lambda f: f

    get = post = _deco


class _Resp:
    def __init__(self, content=None, status_code=200, media_type=None):
        self.content, self.status_code, self.media_type = content, status_code, media_type


class HTMLResponse(_Resp):
    @property
    def text(self):
        return self.content


class RedirectResponse(_Resp):
    def __init__(self, url):
        super().__init__(url, 307)
        self.url = url


class StreamingResponse(_Resp):
    def __init__(self, gen, media_type=None):
        super().__init__(gen, 200, media_type)


def install():
    fastapi = types.ModuleType("fastapi")
    fastapi.Depends = lambda f: f
    fastapi.FastAPI, fastapi.Header, fastapi.HTTPException = FastAPI, (lambda default=None: default), HTTPException
    cors = types.ModuleType("fastapi.middleware.cors")
    cors.CORSMiddleware = object
    middleware = types.ModuleType("fastapi.middleware")
    responses = types.ModuleType("fastapi.responses")
    responses.HTMLResponse, responses.RedirectResponse, responses.StreamingResponse = \
        HTMLResponse, RedirectResponse, StreamingResponse
    pyd = types.ModuleType("pydantic")
    pyd.BaseModel, pyd.Field = BaseModel, Field
    sys.modules.update({"fastapi": fastapi, "fastapi.middleware": middleware, "fastapi.middleware.cors": cors,
                        "fastapi.responses": responses, "pydantic": pyd})
