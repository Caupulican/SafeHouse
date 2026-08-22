"""Architecture gates for the production deployment surfaces."""

from pathlib import Path


def test_ansible_stages_only_current_python_windows_tools():
    root = Path(__file__).resolve().parents[2]
    tasks = (
        root / "ansible" / "roles" / "windows_persistence" / "tasks" / "main.yml"
    ).read_text()
    assert "safehouse_setup" in tasks
    assert "safehouse_ytbudget" in tasks
    assert "install.py" in tasks
    assert "*.ps1" not in tasks
    installer = (root / "python" / "install.py").read_text()
    assert "retire_legacy_runtime()" in installer


def test_deployment_owns_nat_with_a_windows_transport_adapter():
    root = Path(__file__).resolve().parents[2]
    persistence = (
        root / "ansible" / "roles" / "windows_persistence" / "tasks" / "main.yml"
    ).read_text()
    production = "\n".join(
        (root / path).read_text()
        for path in (
            "pihole/docker-compose.yml",
            "adguard/docker-compose.yml",
            "scripts/reconcile-dns.sh",
            "ansible/roles/pihole/templates/env.j2",
            "ansible/roles/adguard/templates/env.j2",
        )
    )

    assert "networkingMode=nat" in persistence
    assert "dnsTunneling=false" in persistence
    assert "Remove superseded mirrored-only WSL settings" in persistence
    assert "state: absent" in persistence
    assert production.count("network_mode: host") == 2
    assert "PIHOLE_BIND_IP" not in production
    assert "ADGUARD_BIND_IP" not in production
    assert "ADGUARD_UPSTREAM=127.0.0.1:5353" in production


def test_windows_profile_detection_is_explicit_utf8_and_fails_closed():
    root = Path(__file__).resolve().parents[2]
    persistence = (
        root / "ansible" / "roles" / "windows_persistence" / "tasks" / "main.yml"
    ).read_text()

    assert "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe" in persistence
    assert "[Text.UTF8Encoding]::new()" in persistence
    assert "winprofile.stdout | trim == ''" in persistence
    assert 'cmd.exe /c' not in persistence


def test_adguard_http_policy_has_one_implementation():
    root = Path(__file__).resolve().parents[2]
    for script in ("adguard/bootstrap.py", "adguard/apply-filters.py"):
        source = (root / script).read_text()
        assert "from adguard_api import api" in source
        assert "def api(" not in source


# --- task XML escaping -------------------------------------------------------
# A `<` in a task description registered nothing and reported only "the XML is malformed"
# with a line and column. Paths are interpolated into the same document, so a directory
# containing `&` would fail identically and just as opaquely.

def test_task_xml_escapes_every_interpolated_value(tmp_path, monkeypatch):
    import install as installmod

    captured = {}

    def fake_run(args):
        if args[:2] == ["schtasks", "/Create"]:
            captured["xml"] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        return 0, ""

    monkeypatch.setattr(installmod, "run", fake_run)
    ok = installmod.register_task(
        name="SafeHouse-Test", python=Path(r"C:\A & B\python.exe"),
        arguments="-m thing --flag <x>", workdir=tmp_path,
        description="dashboard on http://<this machine>:8787 & more",
        start=False,
    )
    assert ok
    xml = captured["xml"]
    # The raw metacharacters must not survive into the document...
    assert "<this machine>" not in xml
    assert "A & B" not in xml
    # ...and must appear as entities instead.
    assert "&lt;this machine&gt;" in xml
    assert "A &amp; B" in xml
    # And the result must still parse, which is the property that actually failed.
    import xml.etree.ElementTree as ET
    ET.fromstring(xml)


# --- the resolver watchdog's task shape --------------------------------------
# Every other task this repo registers runs as SYSTEM. This one cannot, and the reason is
# absolute rather than a preference: Windows refuses to run WSL as LocalSystem at all
# (Wsl/WSL_E_LOCAL_SYSTEM_NOT_SUPPORTED, verified on the live machine by running
# `wsl.exe --list` from a SYSTEM task). A well-meaning future edit that "makes it
# consistent with the others" would silently break the only lane that repairs filtering.

def test_the_resolver_task_runs_as_a_user_and_never_as_system(tmp_path, monkeypatch):
    import install as installmod
    import xml.etree.ElementTree as ET

    captured = {}

    def fake_run(args):
        if args[:2] == ["schtasks", "/Create"]:
            captured["xml"] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        return 0, ""

    monkeypatch.setattr(installmod, "run", fake_run)
    assert installmod.register_resolver(
        python=tmp_path / "python.exe", workdir=tmp_path,
        root=Path(r"C:\ProgramData\SafeHouse"), user="DESKTOP-ABC\\child",
        distro="Debian")

    doc = captured["xml"]
    assert "S-1-5-18" not in doc, "the resolver task must not run as LocalSystem"
    assert "InteractiveToken" in doc
    tree = ET.fromstring(doc)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    principal = tree.find(".//t:Principals/t:Principal", ns)
    # The account on this machine is accented, and the XML is written UTF-16.
    assert principal.find("t:UserId", ns).text == "DESKTOP-ABC\\child"
    # Hidden, because it fires four times an hour on the machine the child is using.
    assert tree.find(".//t:Settings/t:Hidden", ns).text == "true"
    # A logon trigger, because there is no user session to run in before one.
    assert tree.find(".//t:Triggers/t:LogonTrigger", ns) is not None


def test_the_resolver_task_uses_the_windowless_interpreter(tmp_path, monkeypatch):
    """A console window blinking on the child's screen four times an hour gives the whole
    posture away."""
    import install as installmod

    (tmp_path / "pythonw.exe").write_text("")
    captured = {}

    def fake_run(args):
        if args[:2] == ["schtasks", "/Create"]:
            captured["xml"] = Path(args[args.index("/XML") + 1]).read_text(encoding="utf-16")
        return 0, ""

    monkeypatch.setattr(installmod, "run", fake_run)
    installmod.register_resolver(python=tmp_path / "python.exe", workdir=tmp_path,
                                 root=tmp_path, user="DESKTOP-ABC\\u")
    assert "pythonw.exe" in captured["xml"]


def test_a_missing_pythonw_degrades_rather_than_refusing(tmp_path):
    import install as installmod

    assert installmod.windowless(tmp_path / "python.exe").name == "python.exe"
    (tmp_path / "pythonw.exe").write_text("")
    assert installmod.windowless(tmp_path / "python.exe").name == "pythonw.exe"
