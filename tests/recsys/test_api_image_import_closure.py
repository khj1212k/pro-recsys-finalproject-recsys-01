"""docker/api.Dockerfile이 복사하는 파일만으로 API가 임포트되고 기동(lifespan)되는지 본다.

API 이미지는 backend/app과 몇 개 경로만 복사한다. app 패키지가 이미지에 없는 모듈
(backend/scheduler, recommend_engine 전체 등)을 임포트하면 이미지 빌드는 통과해도
컨테이너가 ModuleNotFoundError로 죽는다. 개발 환경과 CI에는 저장소 전체와 편집 가능
설치(ai_workspace, recommend_engine)가 있어 그런 임포트가 조용히 성공한다.

그래서 Dockerfile의 WORKDIR/COPY 줄대로 임시 디렉터리를 만들고, 그 안에서 새 파이썬
프로세스로 app.main을 임포트해 lifespan까지 돌린 뒤, 임시 디렉터리와 site-packages 밖
(= 저장소 체크아웃)에서 읽힌 모듈이 하나도 없는지 확인한다. 실제 이미지에서의 확인은
CI docker-build 잡의 스모크 스텝이 한다.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "docker" / "api.Dockerfile"

PROBE = r"""
import asyncio, json, os, sys

from app.main import app


async def boot():
    async with app.router.lifespan_context(app):
        pass


asyncio.run(boot())

# 기본 피처 함수(recsys_core 서빙 어댑터, ADR 0033)가 이미지 안의 파일만으로 임포트되고 값을 낸다.
import numpy as np
from datetime import datetime, timezone
from types import SimpleNamespace

from app.recsys.config import RecsysConfig
from app.recsys.lgbm_scorer import resolve_feature_fn

fn = resolve_feature_fn(RecsysConfig().feature_fn)
now = datetime(2026, 10, 6, tzinfo=timezone.utc)
item = SimpleNamespace(news_letter_id=1, embedding=np.ones(4, dtype=np.float32), created_at=now, category_id=1)
state = SimpleNamespace(category_ids=[1], hist=None, recent_clicks=[], popularity={})
print("FEATURES=" + json.dumps(list(fn(state, [item], now).shape)))
print("PANDAS=" + json.dumps("pandas" in sys.modules))

repo = os.path.realpath(os.environ["PROBE_REPO_ROOT"]) + os.sep
allowed = tuple(
    os.path.realpath(p) + os.sep for p in (os.getcwd(), sys.prefix, sys.base_prefix)
)
leaked = sorted(
    name
    for name, mod in list(sys.modules.items())
    if getattr(mod, "__file__", None)
    and os.path.realpath(mod.__file__).startswith(repo)
    and not os.path.realpath(mod.__file__).startswith(allowed)
)
print("LEAKED=" + json.dumps(leaked))
"""


def materialize_image_layout(dockerfile: Path, root: Path) -> Path:
    """Dockerfile의 COPY(다른 스테이지에서 가져오는 --from 제외)를 root 아래에 재현하고
    마지막 WORKDIR에 해당하는 경로를 돌려준다."""
    workdir = PurePosixPath("/")
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "WORKDIR":
            workdir = workdir / parts[1]
        elif parts[0] == "COPY" and not any(p.startswith("--from") for p in parts):
            *sources, dest = [p for p in parts[1:] if not p.startswith("--")]
            for src in sources:
                source = REPO_ROOT / src
                target = root / str(workdir / dest).lstrip("/")
                if source.is_dir():
                    shutil.copytree(
                        source,
                        target,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
                else:
                    if dest.endswith("/"):
                        target = target / source.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
    return root / str(workdir).lstrip("/")


def test_api_imports_and_boots_with_only_the_files_the_image_copies(tmp_path):
    app_dir = materialize_image_layout(DOCKERFILE, tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("RECSYS_")}
    env.update(
        # 엔진은 접속을 미루므로(URL 파싱만) 실제 DB 없이 임포트·기동이 된다.
        DATABASE_URL="postgresql://probe:probe@127.0.0.1:1/probe",
        SECRET_KEY="probe",
        ALGORITHM="HS256",
        ACCESS_TOKEN_EXPIRE_MINUTES="30",
        PROBE_REPO_ROOT=str(REPO_ROOT),
    )

    done = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=app_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr[-2000:]
    marker = [ln for ln in done.stdout.splitlines() if ln.startswith("LEAKED=")]
    assert marker, done.stdout[-2000:]
    assert json.loads(marker[-1][len("LEAKED="):]) == []


def test_the_default_feature_function_works_inside_the_image_layout_without_pandas(tmp_path):
    """이미지에 복사되는 recsys_core만으로 서빙 어댑터가 피처를 내고, 그 경로가 pandas를 끌어오지 않는다
    (API 이미지에는 pandas가 없다)."""
    app_dir = materialize_image_layout(DOCKERFILE, tmp_path)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH" and not k.startswith("RECSYS_")}
    env.update(
        DATABASE_URL="postgresql://probe:probe@127.0.0.1:1/probe",
        SECRET_KEY="probe",
        ALGORITHM="HS256",
        ACCESS_TOKEN_EXPIRE_MINUTES="30",
        PROBE_REPO_ROOT=str(REPO_ROOT),
    )

    done = subprocess.run([sys.executable, "-c", PROBE], cwd=app_dir, env=env, capture_output=True, text=True,
                          timeout=120)

    assert done.returncode == 0, done.stderr[-2000:]
    lines = dict(ln.split("=", 1) for ln in done.stdout.splitlines() if "=" in ln)
    assert json.loads(lines["FEATURES"]) == [1, 22]
    assert json.loads(lines["PANDAS"]) is False
    assert json.loads(lines["LEAKED"]) == []
