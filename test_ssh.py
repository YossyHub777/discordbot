from manager.manager import ssh_exec, is_container_running, WINDOWS_COMPOSE
code, out = ssh_exec(f"{WINDOWS_COMPOSE} ps")
print("CODE:", code)
print("OUT:", repr(out))
print("RUNNING:", is_container_running(out, "mochigami"))
