"""Run only the configured reader workspace in the foreground."""
import argparse
import json
from pathlib import Path
import subprocess
import tomllib


_READER_BOOTSTRAP = (
    "import sys; source_root=sys.argv.pop(1); api_path=sys.argv.pop(1); "
    "sys.path.insert(0, source_root); "
    "from warmagent.reader_performance import run_reader; "
    "raise SystemExit(run_reader(api_path, sys.argv[1:]))"
)


def reader_command(config):
    file = Path(config).resolve()
    data = tomllib.loads(file.read_text(encoding="utf-8-sig"))["wechatagent"]
    if not data.get("project_dir"):
        raise ValueError("请先配置wechatagent.project_dir。")
    workspace = Path(data["project_dir"])
    if not workspace.is_absolute():
        workspace = file.parent.parent / workspace
    workspace = workspace.resolve()
    distribution = json.loads((workspace / "config/distribution.json").read_text(encoding="utf-8-sig"))
    python = Path(distribution["python"])
    if not python.is_absolute():
        python = workspace / python
    source = Path(__file__).resolve().parents[1] / "src"
    api = workspace / "scripts" / "reader-api.py"
    return [str(python), "-B", "-c", _READER_BOOTSTRAP, str(source), str(api)], workspace


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/wechatagent.local.toml"))
    args = parser.parse_args(argv)
    command, workspace = reader_command(args.config)
    process = subprocess.Popen(command, cwd=workspace)
    try:
        return process.wait()
    except KeyboardInterrupt:
        # The console also delivers Ctrl+C to the foreground reader.
        try:
            return process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
