# CI/CD + Branch Strategy — ai-livestream-commerce-vn

> Status: **CONFIRMED** (2026-07-11). Companion: `aws-architecture.md` §6.

## 1. Branches

| Branch | Purpose | Who merges | Deploy? |
|---|---|---|---|
| `main` | production-ready only | PR from `develop` or `hotfix/*` | NO auto. Prod via **tag `v*`** or manual |
| `develop` | integration / staging | PR from `feature/*` | **Auto DEV** |
| `feature/*` | one feature / task | PR → `develop` | **CI only** (no deploy) |
| `hotfix/*` | urgent prod fix | PR → `main` AND back-merge `develop` | after merge `main` + tag |

```
feature/foo ──PR──► develop ──PR──► main ──tag v1.2.3──► PROD
                      ▲                │
hotfix/bar ──PR───────┴────────────────┘
```

## 2. Three workflow files

### 2.1 `.github/workflows/ci.yml` — every push / PR

```yaml
name: ci
on:
  push:
    branches: ['**']
  pull_request:
    branches: [main, develop]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install -r liveavatar_api/requirements.txt ruff pytest
      - run: ruff check .
      - run: pytest core/tests/ -q
      - name: Docker build check (no push)
        run: |
          docker build -f services/backend/Dockerfile -t backend:ci .
          # llm-tts / avatar / lmcache same pattern — build only
```

**When:** every branch, every PR.  
**Does:** lint + unit test + Dockerfile syntax. **Never deploys.**

### 2.2 `.github/workflows/deploy-dev.yml` — auto DEV

```yaml
name: deploy-dev
on:
  push:
    branches: [develop]

permissions:
  id-token: write   # OIDC
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: development
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::ACCOUNT:role/GitHubActionsDeployRole-dev
          aws-region: ap-northeast-2
      - name: Login Docker Hub (optional, public push)
        run: echo "${{ secrets.DOCKERHUB_TOKEN }}" | docker login -u ${{ secrets.DOCKERHUB_USER }} --password-stdin
      - name: Build + push 4 public images
        run: |
          TAG=dev-${GITHUB_SHA::7}
          for svc in backend llm-tts avatar lmcache; do
            docker build -f services/$svc/Dockerfile -t imimjusthman/ai-live-$svc:$TAG -t imimjusthman/ai-live-$svc:dev .
            docker push imimjusthman/ai-live-$svc:$TAG
            docker push imimjusthman/ai-live-$svc:dev
          done
      - name: ECS rolling update DEV
        run: |
          for svc in backend llm-tts avatar lmcache; do
            aws ecs update-service --cluster ai-live-dev --service $svc --force-new-deployment
          done
      - name: Smoke
        run: curl -fsS https://dev-api.example.com/api/v1/health/ready
```

**When:** merge/push to `develop` only.  
**Does:** push images → force ECS new deployment on **dev cluster**.

### 2.3 `.github/workflows/deploy-prod.yml` — tag + manual approve

```yaml
name: deploy-prod
on:
  push:
    tags: ['v*']
  workflow_dispatch:

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: production   # GitHub Environment → required reviewers
    steps:
      - uses: actions/checkout@v4
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::ACCOUNT:role/GitHubActionsDeployRole-prod
          aws-region: ap-northeast-2
      - name: Build + push release tags
        run: |
          TAG=${GITHUB_REF_NAME}   # e.g. v1.2.3
          for svc in backend llm-tts avatar lmcache; do
            docker build -f services/$svc/Dockerfile -t imimjusthman/ai-live-$svc:$TAG -t imimjusthman/ai-live-$svc:latest .
            docker push imimjusthman/ai-live-$svc:$TAG
            docker push imimjusthman/ai-live-$svc:latest
          done
      - name: ECS rolling update PROD
        run: |
          for svc in backend llm-tts avatar lmcache; do
            aws ecs update-service --cluster ai-live-prod --service $svc --force-new-deployment
          done
      - name: Wait stable
        run: aws ecs wait services-stable --cluster ai-live-prod --services backend llm-tts avatar
        timeout-minutes: 15
      - name: Rollback on failure
        if: failure()
        run: |
          # restore previous task definition revision
          echo "manual rollback: aws ecs update-service --task-definition <prev-arn>"
```

**When:** `git tag v1.2.3 && git push --tags` (or Actions → Run workflow).  
**Gate:** GitHub Environment `production` requires human approve.  
**Does:** release images → prod ECS. Fail → wait fails → rollback step.

## 3. Concrete day-to-day examples

### Example A — feature work (no prod risk)

```bash
git checkout develop && git pull
git checkout -b feature/run-plan-cursor
# code + tests
git push -u origin feature/run-plan-cursor
# → ci.yml runs (lint/test/build)
# open PR → develop
# merge PR → deploy-dev.yml auto deploys DEV
# QA on DEV
```

### Example B — ship to prod

```bash
# after develop stable
git checkout main
git merge develop   # or PR develop → main
git tag v0.3.0
git push origin main --tags
# → deploy-prod.yml starts
# reviewer clicks Approve in GitHub Environments
# PROD rolls
```

### Example C — hotfix prod

```bash
git checkout main && git pull
git checkout -b hotfix/auth-timing
# fix + test
git push -u origin hotfix/auth-timing
# PR → main, merge
git tag v0.3.1 && git push --tags
# deploy-prod runs
git checkout develop && git merge main   # back-merge so develop not stale
```

### Example D — push feature does NOT deploy prod

```
push feature/foo     → ci.yml only
merge → develop      → ci.yml + deploy-dev.yml
merge → main (no tag)→ ci.yml only (no prod)
tag v* on main       → deploy-prod.yml (+ approve)
```

## 4. OIDC (no long-lived AWS keys)

GitHub mints short JWT → AWS STS `AssumeRoleWithWebIdentity` → temp creds 15–60 min.

Trust condition locks repo + branch/tag:

```
token.actions.githubusercontent.com:sub =
  repo:justHman/ai-livestream-commerce-vn:ref:refs/heads/develop   # dev role
  repo:justHman/ai-livestream-commerce-vn:ref:refs/tags/v*         # prod role
```

Debug fail:
1. Decode job OIDC JWT → check `sub`
2. Match IAM role trust `StringLike` exactly
3. Ensure `permissions: id-token: write` on job
4. OIDC provider `https://token.actions.githubusercontent.com` exists in account

## 5. Image tags convention

| Env | Tags |
|---|---|
| CI only | local `backend:ci` (not pushed) |
| DEV | `imimjusthman/ai-live-{svc}:dev` + `dev-{sha7}` |
| PROD | `imimjusthman/ai-live-{svc}:latest` + `vX.Y.Z` |

Weights **never** in image — S3 `s3://ai-livestream-{env}/weights/` pulled at container start.
