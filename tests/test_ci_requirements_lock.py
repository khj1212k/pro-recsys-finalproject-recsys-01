"""CI 잠금 파일(requirements-ci.txt)이 원본 고정 파일과 어긋나지 않는지 대조한다.

CI는 파이썬 패키지를 잠금 파일 하나로만 설치한다(.github/workflows/ci.yml). 그 파일의 값 중 둘은 다른 곳이 원본이다:
- 추천 평가 리포트를 만든 버전: evaluation/requirements.txt
- API 이미지가 싣는 버전: backend/requirements.txt (fastapi·sqlalchemy·sqlmodel 등)
원본을 고치고 `make ci-lock`을 잊으면 CI가 리포트·이미지와 다른 버전에서 돈다 - 그 사고(PR #26)를 여기서 잡는다.
표준 라이브러리만 쓰고 파일만 읽는다.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# evaluation/requirements.txt의 fastapi는 라벨링 UI용이다. CI의 라우터 테스트는 API 이미지와 같은 fastapi에서 돌아야 하므로
# 이 하나만 backend/requirements.txt 값이 이긴다(requirements-ci.in 머리말).
REPORT_PIN_EXCEPTIONS = {"fastapi"}
API_IMAGE_PINS = ("fastapi", "python-jose", "passlib", "bcrypt", "sqlalchemy", "sqlmodel")

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(path: str) -> dict:
    """requirements 파일에서 `이름==버전` 줄을 {정규화한 이름: {버전}}으로 읽는다(환경 마커가 달린 줄도 센다)."""
    found: dict = {}
    for line in (REPO / path).read_text(encoding="utf-8").splitlines():
        m = _PIN.match(line)  # `# via` 주석 줄은 들여쓰기가 있어 걸리지 않는다
        if m:
            found.setdefault(_norm(m.group(1)), set()).add(m.group(2))
    return found


def _top_level(path: str) -> dict:
    """.in 파일의 최상위 요구사항을 {정규화한 이름: 버전 또는 None}으로 읽는다."""
    found: dict = {}
    for raw in (REPO / path).read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _PIN.match(line)
        if m:
            found[_norm(m.group(1))] = m.group(2)
        else:
            n = _NAME.match(line)
            assert n, f"requirements-ci.in 줄을 읽지 못했습니다: {raw!r}"
            found[_norm(n.group(1))] = None
    return found


def test_every_top_level_requirement_is_in_the_lock():
    lock = _pins("requirements-ci.txt")
    for name, version in _top_level("requirements-ci.in").items():
        assert name in lock, f"{name}이 requirements-ci.txt에 없습니다 - `make ci-lock`을 다시 실행하세요"
        if version is not None:
            assert lock[name] == {version}, (
                f"{name}: requirements-ci.in은 =={version}인데 잠금 파일은 {sorted(lock[name])}입니다 - `make ci-lock`"
            )


def test_lock_matches_the_versions_the_reports_were_made_with():
    lock = _pins("requirements-ci.txt")
    for name, versions in _pins("evaluation/requirements.txt").items():
        if name in REPORT_PIN_EXCEPTIONS:
            continue
        assert lock.get(name) == versions, (
            f"{name}: evaluation/requirements.txt는 {sorted(versions)}, 잠금 파일은 {sorted(lock.get(name, []))} "
            "- 리포트를 만든 버전과 CI가 달라집니다. requirements-ci.in을 고치고 `make ci-lock`"
        )


def test_lock_matches_the_versions_the_api_image_ships():
    lock = _pins("requirements-ci.txt")
    image = _pins("backend/requirements.txt")
    for name in API_IMAGE_PINS:
        assert name in image, f"backend/requirements.txt에 {name}이 없습니다(대조 목록을 고치세요)"
        assert lock.get(name) == image[name], (
            f"{name}: backend/requirements.txt(API 이미지)는 {sorted(image[name])}, "
            f"잠금 파일은 {sorted(lock.get(name, []))}"
        )


def test_ci_workflow_installs_python_packages_only_from_the_lock():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # 단위 테스트 잡과 통합 테스트 잡이 둘 다 잠금 파일로 설치한다.
    assert ci.count("-r requirements-ci.txt") >= 2
    # 잠금 파일 밖에서 SQLAlchemy 계열을 따로 고정하면 다시 설치 목록이 둘이 된다.
    assert "sqlalchemy==" not in ci.lower() and "sqlmodel==" not in ci.lower()
