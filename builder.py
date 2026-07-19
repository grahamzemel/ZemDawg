#!/usr/bin/env python3
"""
Builder pipeline: turns an iMessage prompt into a working Svelte + Node project.

Uses the local `claude` CLI to generate the project, then installs/builds,
optionally deploys the frontend to Netlify, and starts the backend on the
Mac mini (exposed via Cloudflare Tunnel when credentials are available).
"""
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import imessage

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = REPO_ROOT / "logs"
BUILDER_LOG_DIR = LOG_DIR / "builder"
SECRETS_DIR = REPO_ROOT / "secrets"
PROJECTS_BASE = Path(os.environ.get("PROJECTS_DIR", Path.home() / "code"))

CLAUDE_TIMEOUT = 1200  # seconds
NPM_TIMEOUT = 900

LOG = logging.getLogger("builder")
BUILD_LOCK = threading.Lock()
CURRENT_BUILD = {"project": None, "sender": None}


def setup_logging():
    BUILDER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(BUILDER_LOG_DIR / "builder.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def load_secrets():
    """Load secrets/*.env into a dict, without overwriting existing env vars."""
    env = dict(os.environ)
    for name in ["netlify.env", "cloudflare.env", "devin.env"]:
        p = SECRETS_DIR / name
        if not p.exists():
            continue
        try:
            with open(p) as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.match(r"([A-Za-z0-9_]+)\s*=\s*(.*)", line)
                    if not m:
                        continue
                    key, val = m.group(1), m.group(2).strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                        val = val[1:-1]
                    if key not in env:
                        env[key] = val
        except Exception as e:
            LOG.warning("Could not read %s: %s", p, e)
    return env


def send(sender, text):
    """Send an iMessage reply."""
    try:
        imessage.send_imessage(sender, text)
    except Exception as e:
        LOG.error("Failed to send message to %s: %s", sender, e)


def slugify(prompt):
    """Derive a short URL-safe project slug from the prompt."""
    text = prompt.lower()
    text = re.sub(r"^(create|build|make|design|scaffold)\s+(a|an|the|my|me|us)\s*", "", text)
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    words = [w for w in text.split() if w and w not in {"a", "an", "the", "with", "and", "for", "that", "using", "like", "such", "also"}]
    slug = "-".join(words[:5]) or "project"
    slug = re.sub(r"-+", "-", slug).strip("-")
    if len(slug) > 40:
        slug = slug[:40].rsplit("-", 1)[0]
    if not slug:
        slug = "project"
    return slug


def make_project_dir(slug):
    """Create a unique project directory under PROJECTS_BASE."""
    base = PROJECTS_BASE / slug
    if base.exists():
        for i in range(2, 1000):
            base = PROJECTS_BASE / f"{slug}-{i}"
            if not base.exists():
                break
    base.mkdir(parents=True, exist_ok=True)
    return base


def find_public_dir(project_dir):
    """Find the built static site directory (website/public or public)."""
    for candidate in [project_dir / "website" / "public", project_dir / "public"]:
        if candidate.exists() and (candidate / "index.html").exists():
            return candidate
    return None


def find_backend_port(project_dir):
    """Look for an api/server.js or server.js and guess the port."""
    candidates = [project_dir / "api" / "server.js", project_dir / "server.js"]
    for c in candidates:
        if c.exists():
            try:
                text = c.read_text()
                m = re.search(r"listen\s*\(\s*(\d+)", text)
                if m:
                    return int(m.group(1))
            except Exception:
                pass
    return 3001


def run_claude(project_dir, prompt, project_name, env):
    """Run the local Claude CLI to generate a project in project_dir."""
    claude = shutil.which("claude")
    if not claude:
        return False, "claude CLI not found. Install Claude Code or set PATH."

    instruction = """You are a senior full-stack developer building a project for the user.
Build the project directly in the current directory.

USER PROMPT:
{prompt}

REQUIREMENTS:
- The project must be runnable with `npm run dev` from the project root.
- Default stack (use this unless the user explicitly asks for something different): Svelte 4 frontend, Tailwind CSS, Rollup bundler; Node.js backend in a single `api/server.js` file using Express.
- If the user names a frontend framework (e.g. Svelte, React, Vue, Solid), use it. If they name a backend framework/language (e.g. Node/Express, Python/FastAPI, Go), use it. If they don't specify, fall back to the default Svelte + Node/Express.
- Put the frontend in a `website/` directory with `package.json`, bundler config, CSS config, `public/index.html`, `src/main.js` (or `.ts`), and `src/App.svelte`/`.jsx`/`.vue`.
- Put the backend in an `api/` directory with a single main file (`server.js` or `main.py` etc.) and a `package.json`/`requirements.txt`/go.mod.
- Root `package.json` with workspaces `["website", "api"]` and `concurrently` (or an equivalent) so `npm run dev` starts both the frontend dev server and the backend.
- The frontend should fetch from the backend at `http://localhost:<backend-port>` during local dev and at a relative `/api` path in production.
- Do not use Heroku. Frontend deploys to Netlify. Backend runs on the user's Mac mini and is exposed via a Cloudflare Tunnel.
- Include a `netlify.toml` in `website/` that proxies `/api/*` to the backend domain (use a placeholder like `https://api.<project>.grahamzemel.com` or an environment variable).
- Include `scripts/setup-tunnel.sh` and a launchd plist template in `api/` so the backend can be run as a service on the Mac mini.
- Do not ask the user questions. Make reasonable assumptions and keep the code clean and runnable.
- At the very end of your output, include a JSON block (wrapped in triple backticks with `json`) containing: project_name, frontend_local_url, backend_local_url, run_command, notes.
""".format(prompt=prompt)

    cmd = [
        claude,
        "-p",
        instruction,
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--name",
        project_name,
    ]

    LOG.info("Running claude for %s in %s", project_name, project_dir)
    try:
        proc = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        return False, f"claude timed out after {CLAUDE_TIMEOUT}s: {e.stdout or ''}"
    except Exception as e:
        return False, f"claude failed to run: {e}"

    LOG.info("claude stdout length: %d bytes", len(proc.stdout or ""))
    if proc.returncode != 0:
        return False, f"claude exited {proc.returncode}: {proc.stderr or proc.stdout}"

    # Write Claude's output to a log for debugging
    (project_dir / "BUILDER.md").write_text(proc.stdout or "")
    return True, proc.stdout


def run_npm_install(project_dir, env):
    if not (project_dir / "package.json").exists():
        return True, "No root package.json; skipping npm install."
    LOG.info("Running npm install in %s", project_dir)
    proc = subprocess.run(
        ["npm", "install"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=NPM_TIMEOUT,
        env=env,
    )
    if proc.returncode != 0:
        return False, f"npm install failed:\n{proc.stderr or proc.stdout}"
    return True, "npm install completed."


def run_npm_build(project_dir, env):
    pkg = project_dir / "package.json"
    if not pkg.exists():
        return False, "No root package.json found."
    try:
        scripts = json.loads(pkg.read_text()).get("scripts", {})
    except Exception:
        scripts = {}
    if "build" not in scripts:
        return True, "No 'build' script in root package.json; skipping build."
    LOG.info("Running npm run build in %s", project_dir)
    proc = subprocess.run(
        ["npm", "run", "build"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=NPM_TIMEOUT,
        env=env,
    )
    if proc.returncode != 0:
        return False, f"npm run build failed:\n{proc.stderr or proc.stdout}"
    return True, "npm run build completed."


def start_backend(project_dir, project_name, env):
    backend_file = project_dir / "api" / "server.js"
    if not backend_file.exists():
        return None, "No api/server.js found; not starting backend."
    port = find_backend_port(project_dir)
    log_file = project_dir / "logs" / "backend.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("Starting backend for %s on port %d", project_name, port)
    proc = subprocess.Popen(
        ["node", "api/server.js"],
        cwd=project_dir,
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        env={**env, "PORT": str(port), "PROJECT_NAME": project_name},
    )
    return proc, f"Backend started on http://localhost:{port} (pid {proc.pid})"


def deploy_netlify(project_dir, project_name, env):
    token = env.get("NETLIFY_AUTH_TOKEN")
    if not token:
        return None, "NETLIFY_AUTH_TOKEN not set; skipping Netlify deploy."
    netlify = shutil.which("netlify")
    if not netlify:
        return None, "netlify CLI not found; skipping deploy."
    public_dir = find_public_dir(project_dir)
    if not public_dir:
        return None, "No public/index.html found after build; skipping deploy."

    site_name = f"{project_name}-{random.randint(1000, 9999)}"
    LOG.info("Creating Netlify site %s", site_name)
    create = subprocess.run(
        [netlify, "sites:create", "--name", site_name, "--json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    site_id = None
    site_url = None
    try:
        data = json.loads(create.stdout)
        site_id = data.get("id") or data.get("site_id")
        site_url = data.get("url")
    except Exception:
        # Fallback parsing
        m = re.search(r"Site ID:\s+(\S+)", create.stdout)
        if m:
            site_id = m.group(1)
    if not site_id:
        return None, f"Failed to create Netlify site: {create.stderr or create.stdout}"

    LOG.info("Deploying %s to Netlify site %s", public_dir, site_id)
    deploy = subprocess.run(
        [netlify, "deploy", "--prod", "--dir", str(public_dir), "--site", site_id, "--json"],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
    )
    if deploy.returncode != 0:
        return None, f"Netlify deploy failed: {deploy.stderr or deploy.stdout}"
    deploy_url = None
    try:
        data = json.loads(deploy.stdout)
        deploy_url = data.get("deploy_url") or data.get("url") or site_url
    except Exception:
        m = re.search(r"URL:\s+(\S+)", deploy.stdout)
        if m:
            deploy_url = m.group(1)
    return deploy_url, f"Frontend deployed to Netlify: {deploy_url or site_url}"


def create_cloudflare_tunnel(project_name, backend_port, env):
    token = env.get("CLOUDFLARE_API_TOKEN")
    if not token:
        return None, "CLOUDFLARE_API_TOKEN not set; skipping Cloudflare Tunnel."
    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        return None, "cloudflared not found; skipping tunnel."
    zone_id = env.get("CLOUDFLARE_ZONE_ID", "")
    account_id = env.get("CLOUDFLARE_ACCOUNT_ID", "")
    domain = env.get("DOMAIN", "grahamzemel.com")
    hostname = f"api.{project_name}.{domain}"

    # Build env with account id if available
    tunnel_env = dict(env)
    if account_id:
        tunnel_env["CLOUDFLARE_ACCOUNT_ID"] = account_id

    LOG.info("Creating cloudflared tunnel %s", project_name)
    create = subprocess.run(
        [cloudflared, "tunnel", "create", project_name],
        capture_output=True,
        text=True,
        timeout=120,
        env=tunnel_env,
    )
    if create.returncode != 0:
        return None, f"Tunnel creation failed: {create.stderr or create.stdout}"
    tunnel_id = None
    for line in (create.stdout or "").splitlines():
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", line)
        if m:
            tunnel_id = m.group(1)
            break
    if not tunnel_id:
        return None, f"Could not parse tunnel id: {create.stdout}"

    LOG.info("Routing %s to tunnel %s", hostname, tunnel_id)
    route = subprocess.run(
        [cloudflared, "tunnel", "route", "dns", tunnel_id, hostname],
        capture_output=True,
        text=True,
        timeout=120,
        env=tunnel_env,
    )
    if route.returncode != 0:
        return None, f"Tunnel DNS route failed: {route.stderr or route.stdout}"

    # Write a tunnel config file
    creds_file = Path.home() / ".cloudflared" / f"{tunnel_id}.json"
    config_file = Path.home() / ".cloudflared" / f"{project_name}-config.yml"
    config_text = f"""tunnel: {tunnel_id}
credentials-file: {creds_file}
ingress:
  - hostname: {hostname}
    service: http://localhost:{backend_port}
  - service: http_status:404
"""
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(config_text)

    # Start the tunnel in the background
    log_file = Path.home() / ".cloudflared" / f"{project_name}-tunnel.log"
    proc = subprocess.Popen(
        [cloudflared, "tunnel", "--config", str(config_file), "run"],
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        env=tunnel_env,
    )
    return hostname, f"Backend tunneled to https://{hostname} (pid {proc.pid})"


def extract_summary(output):
    """Try to pull the JSON summary block out of Claude's text output."""
    m = re.search(r"```json\n(.*?)\n```", output, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception as e:
            LOG.warning("JSON summary parse failed: %s", e)
    return {}


def build(prompt, sender):
    setup_logging()
    if not BUILD_LOCK.acquire(blocking=False):
        send(sender, f"Already building {CURRENT_BUILD['project']} for {CURRENT_BUILD['sender']}. Wait for it to finish.")
        return

    CURRENT_BUILD["project"] = "starting"
    CURRENT_BUILD["sender"] = sender
    try:
        env = load_secrets()
        project_name = slugify(prompt)
        CURRENT_BUILD["project"] = project_name

        send(sender, f"Building project '{project_name}' from your prompt...")
        project_dir = make_project_dir(project_name)
        LOG.info("Project directory: %s", project_dir)

        # Generate code
        ok, result = run_claude(project_dir, prompt, project_name, env)
        if not ok:
            send(sender, f"Generation failed for {project_name}:\n{result[:1000]}")
            return
        summary = extract_summary(result)
        send(sender, f"Code generated for {project_name}. Installing dependencies...")

        # Install
        ok, msg = run_npm_install(project_dir, env)
        if not ok:
            send(sender, f"Install failed for {project_name}:\n{msg[:1500]}")
            return

        # Build frontend
        ok, msg = run_npm_build(project_dir, env)
        if not ok:
            send(sender, f"Build failed for {project_name}:\n{msg[:1500]}")
            return
        send(sender, f"Build succeeded for {project_name}. Starting backend...")

        # Start backend
        backend_proc, backend_msg = start_backend(project_dir, project_name, env)
        send(sender, backend_msg)

        # Deploy frontend
        netlify_url, netlify_msg = deploy_netlify(project_dir, project_name, env)
        send(sender, netlify_msg)

        # Create tunnel
        port = find_backend_port(project_dir)
        tunnel_host, tunnel_msg = create_cloudflare_tunnel(project_name, port, env)
        send(sender, tunnel_msg)

        # Final summary
        local_frontend = summary.get("frontend_local_url") or "http://localhost:5000"
        local_backend = summary.get("backend_local_url") or f"http://localhost:{port}"
        run_cmd = summary.get("run_command") or "npm run dev"
        notes = summary.get("notes") or ""

        final = f"""Done with '{project_name}'.

Local frontend: {local_frontend}
Local backend: {local_backend}
Run: cd {project_dir} && {run_cmd}
"""
        if netlify_url:
            final += f"Frontend URL: {netlify_url}\n"
        if tunnel_host:
            final += f"Backend URL: https://{tunnel_host}\n"
        if notes:
            final += f"\nNotes: {notes[:500]}\n"
        send(sender, final)
    except Exception as e:
        LOG.exception("Build failed")
        send(sender, f"Build failed: {e}")
    finally:
        CURRENT_BUILD["project"] = None
        CURRENT_BUILD["sender"] = None
        BUILD_LOCK.release()


def start_build(prompt, sender):
    """Entry point called by bridge.py; launches build in a background thread."""
    threading.Thread(target=build, args=(prompt, sender), daemon=True).start()
    return f"Started building '{slugify(prompt)}'. You will get iMessage updates as it progresses."


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: builder.py '<prompt>' [sender]")
        sys.exit(1)
    prompt = sys.argv[1]
    sender = sys.argv[2] if len(sys.argv) > 2 else ""
    build(prompt, sender)
