"""Install the pinned direct Harness runtime and preserve the bound owner.

The catalog probe starts only the local tool host. It makes no model request,
executes no tool, does not run through a shell, and starts no configured MCP
servers. ``--runtime-only`` skips bridge configuration entirely.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HARNESS_VERSION = "0.2.1-rc.2"
MIN_NODE = (24, 0)


def replace_table(text, name, values):
    table = "[" + name + "]\n" + "\n".join(
        f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in values.items()) + "\n\n"
    pattern = r"(?m)^\[" + re.escape(name) + r"\][^\n]*\n[\s\S]*?(?=^\[|\Z)"
    return re.sub(pattern, lambda _: table, text, count=1) if re.search(pattern, text) else text.rstrip() + "\n\n" + table


def _run(argv, *, timeout, check=True, capture_output=False, text=False):
    return subprocess.run([str(value) for value in argv], check=check, timeout=timeout,
        shell=False, capture_output=capture_output, text=text)


def _output(argv, *, timeout=20):
    return subprocess.check_output([str(value) for value in argv], timeout=timeout,
        shell=False, text=True, stderr=subprocess.STDOUT).strip()


def _version_tuple(value):
    match = re.search(r"(?:v)?(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise ValueError("无法读取版本号")
    return tuple(int(part) for part in match.groups())


def _node_version(node):
    value = _output([node, "--version"])
    version = _version_tuple(value)
    if version[:2] < MIN_NODE:
        raise RuntimeError("Node.js 至少需要 24.0.0（B站读取插件要求）")
    return value


def _pnpm_command(args, node):
    if args.pnpm_js:
        script = Path(args.pnpm_js).expanduser().resolve()
        if not script.is_file() or script.suffix.lower() not in {".js", ".cjs", ".mjs"}:
            raise FileNotFoundError("--pnpm-js 必须指向 pnpm JavaScript 入口文件")
        return [str(node), str(script)]
    if not args.pnpm:
        raise ValueError("请提供原有的 --pnpm-js 参数，或提供 --pnpm 命令")
    command = args.pnpm
    candidate = Path(command).expanduser()
    located = candidate.resolve() if candidate.is_file() else shutil.which(command)
    if not located:
        raise FileNotFoundError("找不到 pnpm 命令；请使用 --pnpm-js 指定 pnpm.cjs")
    executable = Path(located).resolve()
    suffix = executable.suffix.lower()
    if suffix in {".js", ".cjs", ".mjs"}:
        return [str(node), str(executable)]
    if suffix in {".cmd", ".bat"}:
        # Windows npm shims point at the real pnpm JavaScript entry. Read only that
        # path and execute it through Node; never execute the shell wrapper.
        wrapper = executable.read_text(encoding="utf-8-sig", errors="replace")
        match = re.search(r"[\"']([^\"'\r\n]*pnpm\.(?:cjs|mjs|js))[\"']", wrapper, re.IGNORECASE)
        if not match:
            raise ValueError("该 pnpm shell shim 无法安全定位 pnpm JavaScript 入口，请改用 --pnpm-js")
        script_name = match.group(1).replace("%~dp0", "").replace('%dp0%', '').lstrip("\\/")
        script = (executable.parent / Path(script_name.replace("\\", os.sep))).resolve()
        if not script.is_file():
            raise FileNotFoundError("pnpm shim 引用的 JavaScript 文件不存在，请改用 --pnpm-js")
        return [str(node), str(script)]
    return [str(executable)]


def _verify_pnpm(command):
    value = _output([*command, "--version"])
    _version_tuple(value)
    # The actual --frozen-lockfile installation owns format compatibility.
    # The pinned v9 lock was also verified with pnpm 11.25.0.
    return value


def _direct_catalog_probe(node, runtime, owner_id):
    async def run():
        from warmagent.direct_harness import DirectHarness

        with tempfile.TemporaryDirectory(prefix="warmagent-harness-probe-") as temporary:
            probe_root = Path(temporary)
            native_config = probe_root / "empty-native-config.json"
            native_config.write_text('{"skills":[],"mcpServers":[]}\n', encoding="utf-8")
            settings = {"enabled": True, "direct_enabled": True, "permission_profile": "full",
                "owner_id": owner_id or "", "node_path": str(node), "runtime_root": str(runtime),
                "native_config": str(native_config), "project_root": str(ROOT)}
            harness = DirectHarness(settings, probe_root / "state", owner_id or "", [], "live")
            try:
                catalog = await harness.catalog()
                if not catalog:
                    raise RuntimeError("DirectHarness 返回空工具目录")
                return sorted(catalog)
            finally:
                await harness.close()

    return asyncio.run(run())


def _owner_id_is_stable(value):
    return (isinstance(value, str) and 0 < len(value) <= 160
            and value.strip().lower() not in {"unknown", "none", "null", "0", "用户"}
            and not any(char in value for char in "|\r\n"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, help="Node.js 24+ 可执行文件")
    package_manager = parser.add_mutually_exclusive_group(required=True)
    package_manager.add_argument("--pnpm-js", help="兼容旧入口：pnpm.cjs/JavaScript 文件")
    package_manager.add_argument("--pnpm", help="pnpm 命令或入口；Windows .cmd shim 只提取 pnpm.cjs")
    parser.add_argument("--full", action="store_true",
        help="兼容旧命令；DirectHarness 已用 full 权限，不启用执行 LLM")
    parser.add_argument("--runtime-only", action="store_true",
        help="仅安装并探测锁定运行时；不要求 owner，也不读取或修改 bridge 配置")
    args = parser.parse_args(argv)

    table = "bridge"
    local = ROOT / "config/bridge.local.toml"
    original = None
    text = None
    bridge = {}
    current = {}
    if not args.runtime_only:
        text = local.read_text(encoding="utf-8-sig")
        original = tomllib.loads(text)
        bridge = original[table]
        owner_id = bridge.get("owner_id")
        if not _owner_id_is_stable(owner_id) or bridge.get("owner_name") != "用户":
            raise ValueError("请先在 bridge.local.toml 配置用户的实际稳定 owner_id 与 owner_name")
        current = bridge.get("computer", {})
        if current.get("owner_id") and current["owner_id"] != owner_id:
            raise ValueError("现有电脑权限 owner_id 不同；先核对身份，不自动改绑")
    else:
        owner_id = ""

    source_node = Path(args.node).expanduser().resolve()
    if not source_node.is_file():
        raise FileNotFoundError("找不到 --node 指定的 Node.js 可执行文件")
    node_version = _node_version(source_node)
    package_command = _pnpm_command(args, source_node)
    pnpm_version = _verify_pnpm(package_command)

    runtime = ROOT / "var/harness/runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    node = ROOT / "var/harness/node/node.exe"
    node.parent.mkdir(parents=True, exist_ok=True)
    if source_node != node.resolve():
        shutil.copyfile(source_node, node)
    for name in ("package.json", "pnpm-workspace.yaml", "pnpm-lock.yaml"):
        source = ROOT / "config/harness" / name
        if not source.is_file():
            raise FileNotFoundError(f"缺少锁定 Harness 文件：{name}")
        shutil.copyfile(source, runtime / name)

    _run([*package_command, "--dir", runtime, "install", "--frozen-lockfile"],
         timeout=300)
    cli = runtime / "node_modules/@deepseek-ai/dsh/lib/bin.js"
    version = _output([node, cli, "--version"])
    if version != EXPECTED_HARNESS_VERSION:
        raise RuntimeError("Harness 锁定版本不匹配")

    tools = _direct_catalog_probe(node, runtime, owner_id)
    probe = {"status": "ready", "tool_count": len(tools), "tools": tools}

    if args.runtime_only:
        restart_required = False
    else:
        settings = {**current, "enabled": True, "direct_enabled": True,
            "agent_enabled": False, "autonomy_granted": True,
            "permission_profile": "full", "owner_id": owner_id,
            "node_path": str(node), "runtime_root": str(runtime)}
        settings.setdefault("native_config", "config/harness/executor.local.json")
        updated = replace_table(text, table + ".computer", settings)
        expected = json.loads(json.dumps(original))
        expected[table]["computer"] = settings
        if tomllib.loads(updated) != expected:
            raise RuntimeError("安装器意外修改了电脑配置之外的项目")
        if local.read_text(encoding="utf-8-sig") != text:
            raise RuntimeError("安装期间 bridge 本地配置发生变化；核对后再重试")
        temporary = local.with_suffix(".toml.computer.tmp")
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(local)
        restart_required = True

    installed = {"harness_version": version, "node_version": node_version,
        "pnpm_version": pnpm_version, "node_sha256": hashlib.sha256(node.read_bytes()).hexdigest(),
        "runtime": str(runtime), "probe": probe, "config_updated": not args.runtime_only,
        "restarted_services": False, "pid": os.getpid(),
        "owner_id_sha256": hashlib.sha256(owner_id.encode()).hexdigest() if owner_id else None,
        "owner_name": "用户" if owner_id else None}
    (ROOT / "var/harness/installation.json").write_text(
        json.dumps(installed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"installed": version, "probe": probe,
        "config_updated": not args.runtime_only, "restart_required": restart_required}, ensure_ascii=False))


if __name__ == "__main__":
    main()
