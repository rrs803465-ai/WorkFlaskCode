from __future__ import annotations

import json
import re
from textwrap import dedent
from typing import Any


def _py_pkg(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "app"


def _static_web(name: str, options: dict[str, str]) -> dict[str, str]:
    title = options.get("title", name)
    return {
        "index.html": dedent(f"""\
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <meta name="viewport" content="width=device-width, initial-scale=1">
              <title>{title}</title>
              <link rel="stylesheet" href="styles.css">
            </head>
            <body>
              <header class="site-header">
                <nav aria-label="Main"><a href="#top" class="brand">{title}</a></nav>
              </header>
              <main id="top">
                <h1>{title}</h1>
                <p>Edit <code>index.html</code>, <code>styles.css</code> and <code>app.js</code> to build your site.</p>
                <form id="contact" novalidate>
                  <label for="email">Email</label>
                  <input id="email" name="email" type="email" required>
                  <button type="submit">Subscribe</button>
                  <p id="status" role="status" aria-live="polite"></p>
                </form>
              </main>
              <footer><small>&copy; {title}</small></footer>
              <script src="app.js" defer></script>
            </body>
            </html>
            """),
        "styles.css": dedent("""\
            :root {
              --color-bg: #ffffff;
              --color-text: #1b1f24;
              --color-accent: #4f46e5;
              --space-1: 0.5rem;
              --space-2: 1rem;
              --space-3: 2rem;
              --font-body: system-ui, -apple-system, "Segoe UI", sans-serif;
            }
            * { box-sizing: border-box; }
            body { margin: 0; background: var(--color-bg); color: var(--color-text); font-family: var(--font-body); line-height: 1.6; }
            .site-header, main, footer { max-width: 60rem; margin: 0 auto; padding: var(--space-2); }
            .brand { color: var(--color-accent); font-weight: 700; text-decoration: none; }
            input, button { font: inherit; padding: var(--space-1); }
            button { background: var(--color-accent); color: #fff; border: 0; border-radius: 0.25rem; cursor: pointer; }
            :focus-visible { outline: 3px solid var(--color-accent); outline-offset: 2px; }
            @media (max-width: 40rem) { main { padding: var(--space-1); } }
            """),
        "app.js": dedent("""\
            const form = document.getElementById("contact");
            const status = document.getElementById("status");
            form.addEventListener("submit", (event) => {
              event.preventDefault();
              const email = form.elements.email.value.trim();
              status.textContent = form.checkValidity() && email ? `Thanks, ${email}.` : "Please enter a valid email address.";
            });
            """),
        "tests/check_site.py": dedent("""\
            import re
            import sys
            from pathlib import Path

            root = Path(__file__).resolve().parent.parent
            html = (root / "index.html").read_text(encoding="utf-8")
            problems = []
            if not re.search(r"<html[^>]*\\blang=", html):
                problems.append("missing lang attribute")
            if "viewport" not in html:
                problems.append("missing viewport meta")
            if re.search(r"<img\\b(?![^>]*\\balt=)", html):
                problems.append("image without alt")
            for ref in re.findall(r'(?:src|href)="([^"#:]+)"', html):
                if not (root / ref).exists():
                    problems.append(f"broken reference: {ref}")
            print("\\n".join(problems) or "site structure ok")
            sys.exit(1 if problems else 0)
            """),
        "README.md": f"# {name}\n\nStatic site. Preview with `python3 -m http.server 8000 --bind 127.0.0.1`.\nCheck structure with `python3 tests/check_site.py`.\n",
        ".gitignore": ".DS_Store\n",
    }


def _python_cli(name: str, options: dict[str, str]) -> dict[str, str]:
    pkg = _py_pkg(name)
    return {
        "pyproject.toml": dedent(f"""\
            [build-system]
            requires = ["setuptools>=68"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "{name}"
            version = "0.1.0"
            description = "{options.get("description", name + " command-line tool")}"
            requires-python = ">=3.10"

            [project.scripts]
            {name} = "{pkg}.cli:main"

            [tool.pytest.ini_options]
            pythonpath = ["src"]
            """),
        f"src/{pkg}/__init__.py": '__version__ = "0.1.0"\n',
        f"src/{pkg}/__main__.py": f"from {pkg}.cli import main\n\nraise SystemExit(main())\n",
        f"src/{pkg}/cli.py": dedent(f"""\
            import argparse
            import sys
            from collections.abc import Sequence

            from {pkg} import __version__


            def greet(who: str, shout: bool = False) -> str:
                message = f"Hello, {{who}}!"
                return message.upper() if shout else message


            def build_parser() -> argparse.ArgumentParser:
                parser = argparse.ArgumentParser(prog="{name}", description="{name} command-line tool")
                parser.add_argument("--version", action="version", version=__version__)
                parser.add_argument("who", nargs="?", default="world")
                parser.add_argument("--shout", action="store_true")
                return parser


            def main(argv: Sequence[str] | None = None) -> int:
                args = build_parser().parse_args(argv)
                print(greet(args.who, args.shout))
                return 0


            if __name__ == "__main__":
                sys.exit(main())
            """),
        "tests/test_cli.py": dedent(f"""\
            import pytest

            from {pkg}.cli import greet, main


            def test_greet_default():
                assert greet("world") == "Hello, world!"


            def test_greet_shout():
                assert greet("ada", shout=True) == "HELLO, ADA!"


            def test_main_prints(capsys):
                assert main(["ada"]) == 0
                assert capsys.readouterr().out.strip() == "Hello, ada!"


            def test_version_flag(capsys):
                with pytest.raises(SystemExit) as exc:
                    main(["--version"])
                assert exc.value.code == 0
            """),
        "README.md": f"# {name}\n\n```\npip install -e .\n{name} --help\npytest\n```\n",
        ".gitignore": "__pycache__/\n*.egg-info/\n.venv/\n.pytest_cache/\ndist/\nbuild/\n",
    }


def _flask(name: str, options: dict[str, str]) -> dict[str, str]:
    pkg = _py_pkg(name)
    return {
        "requirements.txt": "flask>=3.0,<4\n",
        "requirements-dev.txt": "-r requirements.txt\npytest>=8\n",
        f"{pkg}/__init__.py": dedent("""\
            import os
            import sqlite3

            from flask import Flask, g, jsonify, request


            def create_app(config: dict | None = None) -> Flask:
                app = Flask(__name__)
                app.config.update(DATABASE=os.environ.get("DATABASE_URL", "app.db"), SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"))
                if config:
                    app.config.update(config)

                def db() -> sqlite3.Connection:
                    if "db" not in g:
                        g.db = sqlite3.connect(app.config["DATABASE"])
                        g.db.row_factory = sqlite3.Row
                        g.db.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)")
                    return g.db

                @app.teardown_appcontext
                def close_db(_exc):
                    conn = g.pop("db", None)
                    if conn is not None:
                        conn.close()

                @app.get("/health")
                def health():
                    return jsonify(status="ok")

                @app.get("/items")
                def list_items():
                    return jsonify([dict(r) for r in db().execute("SELECT id, name FROM items ORDER BY id")])

                @app.post("/items")
                def add_item():
                    payload = request.get_json(silent=True) or {}
                    name = str(payload.get("name", "")).strip()
                    if not name:
                        return jsonify(error="name is required"), 400
                    cur = db().execute("INSERT INTO items(name) VALUES (?)", (name,))
                    db().commit()
                    return jsonify(id=cur.lastrowid, name=name), 201

                return app
            """),
        "wsgi.py": f"from {pkg} import create_app\n\napp = create_app()\n",
        "tests/test_app.py": dedent(f"""\
            import pytest

            from {pkg} import create_app


            @pytest.fixture
            def client(tmp_path):
                app = create_app({{"DATABASE": str(tmp_path / "t.db"), "TESTING": True}})
                return app.test_client()


            def test_health(client):
                assert client.get("/health").get_json() == {{"status": "ok"}}


            def test_create_and_list(client):
                assert client.post("/items", json={{"name": "a"}}).status_code == 201
                assert [i["name"] for i in client.get("/items").get_json()] == ["a"]


            def test_validation(client):
                assert client.post("/items", json={{}}).status_code == 400
            """),
        "README.md": f"# {name}\n\n```\npip install -r requirements-dev.txt\nflask --app wsgi run --host 127.0.0.1 --port 5000\npytest\n```\n",
        ".gitignore": "__pycache__/\n.venv/\n*.db\n.env\n.pytest_cache/\n",
    }


def _fastapi(name: str, options: dict[str, str]) -> dict[str, str]:
    pkg = _py_pkg(name)
    return {
        "requirements.txt": "fastapi>=0.110,<1\nuvicorn>=0.27,<1\n",
        "requirements-dev.txt": "-r requirements.txt\npytest>=8\nhttpx>=0.27,<1\n",
        f"{pkg}/__init__.py": "",
        f"{pkg}/main.py": dedent("""\
            from fastapi import FastAPI, HTTPException
            from pydantic import BaseModel, Field

            app = FastAPI(title="API")
            _items: dict[int, dict] = {}


            class ItemIn(BaseModel):
                name: str = Field(min_length=1, max_length=120)


            @app.get("/health")
            def health() -> dict:
                return {"status": "ok"}


            @app.post("/items", status_code=201)
            def create_item(item: ItemIn) -> dict:
                new_id = max(_items, default=0) + 1
                _items[new_id] = {"id": new_id, "name": item.name}
                return _items[new_id]


            @app.get("/items/{item_id}")
            def read_item(item_id: int) -> dict:
                if item_id not in _items:
                    raise HTTPException(status_code=404, detail="item not found")
                return _items[item_id]
            """),
        "tests/test_api.py": dedent(f"""\
            from fastapi.testclient import TestClient

            from {pkg}.main import app

            client = TestClient(app)


            def test_health():
                assert client.get("/health").json() == {{"status": "ok"}}


            def test_create_and_read():
                created = client.post("/items", json={{"name": "a"}})
                assert created.status_code == 201
                assert client.get(f"/items/{{created.json()['id']}}").json()["name"] == "a"


            def test_missing_item():
                assert client.get("/items/99999").status_code == 404
            """),
        "README.md": f"# {name}\n\n```\npip install -r requirements-dev.txt\nuvicorn {pkg}.main:app --host 127.0.0.1 --port 8000\npytest\n```\n",
        ".gitignore": "__pycache__/\n.venv/\n.env\n.pytest_cache/\n",
    }


def _express(name: str, options: dict[str, str]) -> dict[str, str]:
    pkg: dict[str, Any] = {
        "name": name.lower(),
        "version": "0.1.0",
        "private": True,
        "type": "commonjs",
        "scripts": {"start": "node src/server.js", "test": "node --test tests/"},
        "dependencies": {"express": "^4.19.2"},
    }
    return {
        "package.json": json.dumps(pkg, indent=2) + "\n",
        "src/app.js": dedent("""\
            const express = require("express");

            function createApp() {
              const app = express();
              const items = [];
              app.use(express.json({ limit: "100kb" }));
              app.get("/health", (_req, res) => res.json({ status: "ok" }));
              app.get("/items", (_req, res) => res.json(items));
              app.post("/items", (req, res) => {
                const name = typeof req.body?.name === "string" ? req.body.name.trim() : "";
                if (!name) return res.status(400).json({ error: "name is required" });
                const item = { id: items.length + 1, name };
                items.push(item);
                return res.status(201).json(item);
              });
              return app;
            }

            module.exports = { createApp };
            """),
        "src/server.js": dedent("""\
            const { createApp } = require("./app");

            const host = process.env.HOST || "127.0.0.1";
            const port = Number(process.env.PORT || 3000);
            createApp().listen(port, host, () => console.log(`listening on http://${host}:${port}`));
            """),
        "tests/app.test.js": dedent("""\
            const test = require("node:test");
            const assert = require("node:assert");
            const { createApp } = require("../src/app");

            test("health and item creation", async () => {
              const server = createApp().listen(0, "127.0.0.1");
              await new Promise((resolve) => server.once("listening", resolve));
              const base = `http://127.0.0.1:${server.address().port}`;
              try {
                assert.deepStrictEqual(await (await fetch(`${base}/health`)).json(), { status: "ok" });
                const created = await fetch(`${base}/items`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ name: "a" }) });
                assert.strictEqual(created.status, 201);
                const bad = await fetch(`${base}/items`, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
                assert.strictEqual(bad.status, 400);
              } finally {
                server.close();
              }
            });
            """),
        "README.md": f"# {name}\n\n```\nnpm install\nnpm start\nnpm test\n```\n",
        ".gitignore": "node_modules/\n.env\n",
    }


def _vite_react(name: str, options: dict[str, str]) -> dict[str, str]:
    pkg = {
        "name": name.lower(),
        "private": True,
        "version": "0.1.0",
        "type": "module",
        "scripts": {
            "dev": "vite --host 127.0.0.1",
            "build": "vite build",
            "preview": "vite preview --host 127.0.0.1",
            "test": "vitest run",
        },
        "dependencies": {"react": "^18.3.1", "react-dom": "^18.3.1"},
        "devDependencies": {
            "@vitejs/plugin-react": "^4.3.1",
            "vite": "^5.4.0",
            "vitest": "^2.0.5",
            "jsdom": "^25.0.0",
            "@testing-library/react": "^16.0.0",
        },
    }
    return {
        "package.json": json.dumps(pkg, indent=2) + "\n",
        "vite.config.js": dedent("""\
            import { defineConfig } from "vite";
            import react from "@vitejs/plugin-react";

            export default defineConfig({
              plugins: [react()],
              server: { host: "127.0.0.1", port: 5173 },
              test: { environment: "jsdom" },
            });
            """),
        "index.html": f'<!doctype html>\n<html lang="en">\n  <head>\n    <meta charset="UTF-8" />\n    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n    <title>{name}</title>\n  </head>\n  <body>\n    <div id="root"></div>\n    <script type="module" src="/src/main.jsx"></script>\n  </body>\n</html>\n',
        "src/main.jsx": 'import React from "react";\nimport { createRoot } from "react-dom/client";\nimport App from "./App.jsx";\nimport "./styles.css";\n\ncreateRoot(document.getElementById("root")).render(<App />);\n',
        "src/App.jsx": dedent(f"""\
            import {{ useState }} from "react";

            export default function App() {{
              const [count, setCount] = useState(0);
              return (
                <main>
                  <h1>{name}</h1>
                  <button type="button" onClick={{() => setCount((c) => c + 1)}}>
                    Clicked {{count}} times
                  </button>
                </main>
              );
            }}
            """),
        "src/App.test.jsx": dedent("""\
            import { render, screen, fireEvent } from "@testing-library/react";
            import { expect, test } from "vitest";
            import App from "./App.jsx";

            test("counter increments", () => {
              render(<App />);
              fireEvent.click(screen.getByRole("button"));
              expect(screen.getByRole("button").textContent).toBe("Clicked 1 times");
            });
            """),
        "src/styles.css": ":root { --color-accent: #4f46e5; --space: 1rem; }\nbody { margin: 0; font-family: system-ui, sans-serif; }\nmain { max-width: 48rem; margin: 0 auto; padding: var(--space); }\nbutton { background: var(--color-accent); color: #fff; border: 0; border-radius: 0.25rem; padding: 0.5rem 1rem; }\n",
        "README.md": f"# {name}\n\n```\nnpm install\nnpm run dev\nnpm test\nnpm run build\n```\n",
        ".gitignore": "node_modules/\ndist/\n.env\n",
    }


def _php(name: str, options: dict[str, str]) -> dict[str, str]:
    return {
        "public/index.php": dedent("""\
            <?php
            declare(strict_types=1);

            require __DIR__ . '/../src/router.php';

            route($_SERVER['REQUEST_METHOD'] ?? 'GET', parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/');
            """),
        "src/router.php": dedent("""\
            <?php
            declare(strict_types=1);

            function route(string $method, string $path): void
            {
                header('Content-Type: application/json');
                if ($method === 'GET' && $path === '/health') {
                    echo json_encode(['status' => 'ok']);
                    return;
                }
                http_response_code(404);
                echo json_encode(['error' => 'not found']);
            }
            """),
        "README.md": f"# {name}\n\n```\nphp -S 127.0.0.1:8080 -t public\n```\n",
        ".gitignore": "vendor/\n.env\n",
    }


def all_specs(spec_cls: Any) -> list[Any]:
    return [
        spec_cls(
            "static-web",
            "HTML/CSS/JavaScript site with accessible form and structure check",
            _static_web,
            ["python3", "tests/check_site.py"],
            "python3 -m http.server 8000 --bind 127.0.0.1",
            [],
            ("website", "landing", "html", "static"),
        ),
        spec_cls(
            "python-cli",
            "Packaged Python command-line tool with pytest tests",
            _python_cli,
            ["python3", "-m", "pytest", "-q"],
            "pip install -e . && <name> --help",
            [],
            ("cli", "python", "command"),
        ),
        spec_cls(
            "flask",
            "Flask JSON API with SQLite and tests",
            _flask,
            ["python3", "-m", "pytest", "-q"],
            "flask --app wsgi run --host 127.0.0.1",
            ["needs flask installed to run tests"],
            ("flask", "api", "backend", "python"),
        ),
        spec_cls(
            "fastapi",
            "FastAPI service with pydantic validation and tests",
            _fastapi,
            ["python3", "-m", "pytest", "-q"],
            "uvicorn <pkg>.main:app --host 127.0.0.1",
            ["needs fastapi, httpx installed to run tests"],
            ("fastapi", "api", "backend", "python"),
        ),
        spec_cls(
            "express",
            "Express JSON API with node:test tests",
            _express,
            ["npm", "test"],
            "npm start",
            ["needs npm install"],
            ("express", "node", "api", "backend", "javascript"),
        ),
        spec_cls(
            "vite-react",
            "React + Vite app with Vitest",
            _vite_react,
            ["npm", "test"],
            "npm run dev",
            ["needs npm install", "never built or rendered by WorkFlaskCode"],
            ("react", "vite", "frontend", "spa"),
        ),
        spec_cls(
            "php",
            "Minimal PHP front controller",
            _php,
            None,
            "php -S 127.0.0.1:8080 -t public",
            ["no tests generated", "needs php installed"],
            ("php",),
        ),
    ]


def addon_files(addon: str, name: str, template: str) -> dict[str, str]:
    is_node = template in ("express", "vite-react")
    is_py = template in ("flask", "fastapi", "python-cli")
    slug = name.lower()
    if addon == "docker":
        if is_node:
            body = dedent("""\
                FROM node:20.17-alpine
                WORKDIR /app
                COPY package*.json ./
                RUN npm ci --omit=dev || npm install --omit=dev
                COPY . .
                USER node
                ENV HOST=0.0.0.0 PORT=3000
                EXPOSE 3000
                CMD ["npm", "start"]
                """)
        elif is_py:
            body = dedent("""\
                FROM python:3.12-slim
                WORKDIR /app
                COPY requirements.txt* ./
                RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi
                COPY . .
                RUN useradd --create-home app && chown -R app /app
                USER app
                EXPOSE 8000
                CMD ["python3", "-m", "http.server", "8000"]
                """)
            if template == "flask":
                body = body.replace(
                    "pip install --no-cache-dir -r requirements.txt; fi",
                    "pip install --no-cache-dir -r requirements.txt gunicorn; fi",
                ).replace(
                    'CMD ["python3", "-m", "http.server", "8000"]',
                    'CMD ["gunicorn", "-b", "0.0.0.0:8000", "wsgi:app"]',
                )
            if template == "fastapi":
                body = body.replace(
                    'CMD ["python3", "-m", "http.server", "8000"]',
                    f'CMD ["uvicorn", "{_py_pkg(name)}.main:app", "--host", "0.0.0.0", "--port", "8000"]',
                )
        else:
            body = "FROM nginx:1.27-alpine\nCOPY . /usr/share/nginx/html\nEXPOSE 80\n"
        return {"Dockerfile": body, ".dockerignore": ".git\nnode_modules\n.venv\n__pycache__\n.env\n"}
    if addon == "compose":
        port = "3000" if is_node else "8000" if is_py else "8080"
        return {
            "docker-compose.yml": dedent(f"""\
            services:
              {slug}:
                build: .
                ports:
                  - "127.0.0.1:{port}:{"80" if not (is_node or is_py) else port}"
                env_file:
                  - .env
                restart: unless-stopped
            """)
        }
    if addon == "ci":
        if is_node:
            steps = "      - uses: actions/setup-node@v4\n        with:\n          node-version: 20\n      - run: npm ci || npm install\n      - run: npm test\n"
        elif is_py:
            steps = "      - uses: actions/setup-python@v5\n        with:\n          python-version: '3.12'\n      - run: pip install -r requirements-dev.txt || pip install pytest && pip install -e .\n      - run: python -m pytest -q\n"
        else:
            steps = "      - run: python3 tests/check_site.py\n"
        return {
            ".github/workflows/ci.yml": f"name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n{steps}"
        }
    if addon == "systemd":
        exec_start = (
            "/usr/bin/npm start"
            if is_node
            else f"/opt/{slug}/.venv/bin/python -m {_py_pkg(name)}"
            if template == "python-cli"
            else "/opt/" + slug + "/run.sh"
        )
        return {
            f"deploy/{slug}.service": dedent(f"""\
            [Unit]
            Description={name}
            After=network.target

            [Service]
            Type=simple
            User={slug}
            WorkingDirectory=/opt/{slug}
            EnvironmentFile=/etc/{slug}/env
            ExecStart={exec_start}
            Restart=on-failure
            NoNewPrivileges=true
            ProtectSystem=strict
            ProtectHome=true
            PrivateTmp=true

            [Install]
            WantedBy=multi-user.target
            """)
        }
    return {
        ".env.example": "# Copy to .env and fill in. Never commit .env.\nHOST=127.0.0.1\nPORT=8000\nSECRET_KEY=change-me\nDATABASE_URL=app.db\n"
    }
