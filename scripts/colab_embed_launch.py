"""Colab 커널 안에서 기사 임베딩 드라이버를 앞(foreground)에서 띄우는 작은 런처 - 표준 라이브러리만 쓴다.

    colab exec -s <세션> --timeout 3600 \
        --env E0EMB_CODE_SHA256=<tarball sha256> \
        --env 'E0EMB_DRIVER_ARGS_JSON=["run","--workdir","/content/e0emb","--code-tarball","/content/e0emb/code.tar.gz",
               "--code-sha256","<sha256>","--export-identity","<identity_sha256>","--cu-rate","1.07","--cu-cap","4"]' \
        -f scripts/colab_embed_launch.py

하는 일: 올려 둔 코드 tarball의 sha256을 확인하고(다르면 아무것도 실행하지 않는다) 그 안의 드라이버 두 파일만 꺼내
`E0EMB_DRIVER_ARGS_JSON`의 인자로 드라이버를 돌린다. `colab exec`의 클라이언트가 붙어 있는 동안 세션이 유휴로 회수되지
않으므로, 15분 안팎의 임베딩 잡은 분리 실행·keeper 없이 이 런처 하나로 끝난다(더 길면 드라이버의 --detach와 keeper를 쓴다).
드라이버의 출력(건수·해시·시간뿐이다)을 그대로 보여 주고, 마지막 줄에 종료 코드를 적는다.
"""
import hashlib
import json
import os
import subprocess
import sys
import tarfile

DRIVER_FILES = ("scripts/colab_embed_articles.py", "scripts/m4_colab_driver.py")


def launch(env=None, *, run=subprocess.run) -> int:
    env = dict(os.environ if env is None else env)
    workdir = env.get("E0EMB_WORKDIR", "/content/e0emb")
    tarball = env.get("E0EMB_CODE_TARBALL", os.path.join(workdir, "code.tar.gz"))
    want = (env.get("E0EMB_CODE_SHA256") or "").lower()
    if not want or "E0EMB_DRIVER_ARGS_JSON" not in env:
        print("E0EMB_LAUNCH " + json.dumps({"error": "E0EMB_CODE_SHA256과 E0EMB_DRIVER_ARGS_JSON이 필요하다"}))
        return 64
    digest = hashlib.sha256()
    with open(tarball, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    if digest.hexdigest() != want:
        print("E0EMB_LAUNCH " + json.dumps({"error": "코드 tarball sha256 불일치 - 아무것도 실행하지 않았다"}))
        return 2
    target = os.path.join(workdir, "drv")
    with tarfile.open(tarball) as tf:
        for name in DRIVER_FILES:
            member = tf.getmember(name)
            if not member.isfile():
                print("E0EMB_LAUNCH " + json.dumps({"error": f"{name}이 일반 파일이 아니다"}))
                return 2
            os.makedirs(os.path.join(target, "scripts"), exist_ok=True)
            with open(os.path.join(target, name), "wb") as out:
                out.write(tf.extractfile(member).read())
    result = run([sys.executable, os.path.join(target, DRIVER_FILES[0])], env=env, cwd=workdir)
    print("E0EMB_LAUNCH " + json.dumps({"returncode": result.returncode, "tarball_sha256": want}))
    return result.returncode


if __name__ == "__main__":
    launch()
