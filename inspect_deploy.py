import inspect
from google.agents.cli.deploy import agent_runtime

lines = inspect.getsource(agent_runtime.deploy_agent_runtime).splitlines()
for i, line in enumerate(lines[240:320], start=240):
    print(f"{i:2d}: {line}")
