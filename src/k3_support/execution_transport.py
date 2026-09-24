"""Operator-selected execution location, pinned in each broker command plan."""


def target(config):
    runtime = config.raw["runtime"]
    mode = runtime.get("remote_transport", "ssh")
    if mode not in ("ssh", "local"):
        raise ValueError("unknown execution transport")
    host = config.runtime("remote_host")
    if mode == "local" and host != "localhost":
        raise ValueError("local execution requires an explicit localhost target")
    return {"transport": mode, "host": host, "ssh_command": config.runtime("ssh_command")}


def matches(config, plan):
    expected = target(config)
    # Historical plans always meant SSH. Never reinterpret them as local work.
    return (plan.get("transport", "ssh") == expected["transport"]
            and all(plan.get(key) == expected[key] for key in ("host", "ssh_command")))


def plan_argv(plan, command, *, strict=True):
    mode = plan.get("transport", "ssh")
    if not isinstance(command, str) or not command or "\0" in command:
        raise ValueError("execution command required")
    if mode == "local":
        if plan.get("host") != "localhost":
            raise ValueError("local execution target changed")
        # The command is the same control-built sandbox/guardian program used
        # over SSH. This is not a new raw-shell interface for worker requests.
        return ["/bin/bash", "--noprofile", "--norc", "-c", command]
    if mode != "ssh":
        raise ValueError("unknown execution transport")
    options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"] if strict else []
    return [plan["ssh_command"], *options, plan["host"], command]


def command_argv(config, command, *, strict=False):
    return plan_argv(target(config), command, strict=strict)
