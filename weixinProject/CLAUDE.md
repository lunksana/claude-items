# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

WeChat Mini Program (微信小程序) for **S2b2c social commerce** — platform supplies products, logistics, and payments; small agents (代理商) promote products through their WeChat social graph and earn commission on sales; end consumers buy through the Mini Program.

Reference products: 微店 (Weidian), 云集 (Yunji), 贝店 (Beidian).

## ⚠️ Compliance Red Line: 二级分销

WeChat platform rules and PRC law cap commission distribution at **two levels maximum** (一级销售 + 一级代理). Designs with **three or more commission tiers** are classified as 传销 (pyramid scheme) and will result in:
- Mini Program being taken down by WeChat (无法过审 / 下架)
- Legal exposure under 《禁止传销条例》

**Apply this everywhere:**
- Commission ledger schema must structurally prevent recording a 3rd-tier payout (e.g., enforce at DB level, not just in business logic).
- "Agent invites agent" features may pay the inviter **only** on the invitee's direct sales — never on the invitee's downstream agent sales.
- No "团队业绩" (team performance) rewards that aggregate across multiple downstream layers.
- Reviewer checklist: any PR touching `commission`, `agent`, `distribution`, `分销`, `返佣` must explicitly state which tier the payout is on.

## Architecture

### Frontend — WeChat Mini Program

- WXML / WXSS / JS (or TypeScript) using the official Mini Program framework.
- Likely uses `wx.login` → `code2Session` → `openid` + `unionid` for identity.
- Sharing flow is the growth engine: `onShareAppMessage` + poster generation (`wx.canvasToTempFilePath`) for agents to share to WeChat moments / chats.

### Backend — Self-Hosted (本地优先，后续迁移云端)

**Decision**: self-hosted independent backend for local development, with a planned migration to a cloud server later. **微信云开发 (CloudBase) is explicitly NOT used** — avoid CloudBase-specific APIs (`wx.cloud.*`, cloud functions, cloud DB SDK) anywhere in the Mini Program code.

To keep the future cloud migration cheap:
- All config via environment variables (`.env` locally, secrets manager in cloud) — no hard-coded hosts, ports, or paths.
- Containerize early (Dockerfile + docker-compose for local DB + app) so the cloud move is mostly "run the same image."
- Use a managed-friendly database (MySQL or PostgreSQL) — avoid SQLite for anything beyond throwaway prototyping, since the commission ledger needs real transactions.
- File uploads (商品图片, 分享海报) go through an abstraction (S3-compatible interface) — local backend can use MinIO or filesystem; cloud will swap to 腾讯云 COS / 阿里云 OSS.
- WeChat Pay `notify_url` is a public HTTPS endpoint — during local dev use ngrok/frp; in cloud, point at the deployed domain. The handler code stays identical.

**Stack**: Python 3.11+ + **FastAPI** + **SQLAlchemy** (2.x, async) + Alembic (migrations) + Pydantic v2 (schemas) + uvicorn (ASGI). Database: PostgreSQL (preferred) or MySQL. Object storage abstraction via `boto3` (S3-compatible) talking to local MinIO in dev.

Conventions:
- Project layout: `app/` with `api/` (routers), `models/` (SQLAlchemy ORM), `schemas/` (Pydantic), `services/` (business logic), `core/` (config, db, security), `tasks/` (async/background jobs).
- Dependency management: `uv` or `poetry` — pick one and stick with it; pin via lockfile.
- All DB writes for commission events go through a single `ledger_service` that enforces the **二级分销 tier constraint** (see red-line section above) — never write to the ledger table directly from a router.

### Core Domains

1. **Identity & Agents** — `openid`/`unionid`, agent tier (普通用户 / 代理商), inviter relation (one level only, see red line above).
2. **Catalog & Inventory** — products, SKUs, stock, supplier (the "S" in S2b2c).
3. **Orders & Payment** — WeChat Pay (`wx.requestPayment`), order state machine, refunds.
4. **Commission Ledger** — append-only ledger of commission events; each row has `order_id`, `agent_id`, `tier` (constrained to 1 or 2), `amount`, `status` (pending / settled / clawed-back on refund).
5. **Settlement** — periodic payout to agent's WeChat 零钱 via 企业付款到零钱 / 商家转账到零钱 API.

### Key Config Values (environment variables, never in source)

- `WECHAT_MINIAPP_APPID` / `WECHAT_MINIAPP_SECRET` — Mini Program credentials
- `WECHAT_PAY_MCH_ID` / `WECHAT_PAY_API_V3_KEY` / `WECHAT_PAY_CERT_PATH` — WeChat Pay v3
- `WECHAT_PAY_NOTIFY_URL` — payment callback (must be HTTPS, publicly reachable)

## Common Commands

_Fill in once the stack is scaffolded._

```bash
# Backend (FastAPI) — fill in once scaffolded
# uv sync                           # install deps (or `poetry install`)
# uv run uvicorn app.main:app --reload --port 8000
# uv run alembic revision --autogenerate -m "msg"
# uv run alembic upgrade head
# uv run pytest                     # run tests
# uv run pytest tests/test_x.py::test_y -x   # single test
# uv run ruff check . && uv run ruff format .

# Local infra
# docker compose up -d              # postgres + minio + redis (if used)

# Mini Program frontend
# Build/preview/upload via WeChat DevTools (微信开发者工具) GUI, or its CLI.
```

## Development Notes

- **Local debugging**: WeChat DevTools simulates the Mini Program runtime. For real-device testing, the backend domain must be added to the Mini Program's 服务器域名 whitelist (HTTPS only, ICP-备案 required).
- **Payment callback** cannot hit localhost — use ngrok / frp / a staging server with a real domain.
- **Audit trail** for commission events is non-negotiable: every payout must be reproducible from the ledger, because refunds trigger clawbacks and disputes will happen.
