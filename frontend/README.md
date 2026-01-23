### Front-End React 실행 가이드

#### 1. Node.js 및 npm 설치
이 프로젝트는 Node.js 기반으로 실행됩니다.
공식 홈페이지에서 다운로드하거나, **터미널(Bash)**을 통해 설치할 수 있습니다.

**방법 A: nvm(Node Version Manager)으로 설치 (권장)**
터미널에서 아래 명령어를 입력하여 nvm을 설치하고 Node.js LTS 버전을 설정합니다.
```bash
# nvm 설치
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash

# 터미널 재시작 후 Node.js LTS 버전 설치
nvm install --lts
```

**방법 B: 공식 홈페이지 다운로드**
- [Node.js 공식 홈페이지 바로가기](https://nodejs.org/)에서 LTS 버전을 다운로드하여 설치합니다.

설치 후 터미널(터미널, CMD, PowerShell 등)에서 아래 명령어를 입력하여 정상적으로 설치되었는지 확인합니다.
```bash
node -v
npm -v
```

#### 2. 리포지토리 클론 (Clone Repository)
원하는 로컬 폴더에서 아래 명령어를 실행하여 프로젝트 코드를 내려받습니다.
```bash
git clone https://github.com/your-username/your-repo.git
```

#### 3. 프로젝트 디렉토리로 이동
내려받은 프로젝트 폴더 안으로 이동합니다.
```bash
cd your-repo
```

#### 4. 의존성 패키지 설치 (Install Dependencies)
프로젝트 실행에 필요한 라이브러리(node_modules)를 설치합니다.
```bash
npm install
```

#### 5. 개발 서버 실행 (Start Development Server)
로컬 환경에서 프로젝트를 실행합니다.
명령어 실행 후 터미널에 표시되는 로컬 주소(예: `http://localhost:8080`)로 접속하면 화면을 볼 수 있습니다.
```bash
npm run dev
```