"""Read findash secrets from the consolidated `.secrets/findash` file (INI
sections). Stdlib-only, pure parsing — used by send_telegram.sh and unit-tested
in test_findash_secrets.py."""


def parse_ini(text):
    """Parse a minimal INI: ``[section]`` headers and ``key=value`` lines, with
    ``#`` / ``;`` comments. Lines before the first header live under the ''
    (default) section."""
    out = {"": {}}
    section = ""
    for raw in text.split("\n"):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            out.setdefault(section, {})
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            out[section][key.strip()] = value.strip()
    return out


def read_section(section, path=".secrets/findash"):
    """Return ``{key: value}`` for one ``[section]`` of the consolidated secrets
    file. An absent file yields ``{}``."""
    try:
        with open(path, encoding="utf-8") as f:
            return parse_ini(f.read()).get(section, {})
    except FileNotFoundError:
        return {}
