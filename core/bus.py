"""In-process message bus. All agent-to-agent traffic goes through dispatch(),
so every Task and Result is logged."""
from core.messages import Task, Result


class MessageBus:
    def __init__(self, logger):
        self.logger = logger
        self.agents = {}

    def register(self, agent):
        self.agents[agent.name] = agent

    def dispatch(self, task: Task) -> Result:
        self.logger.log(task.sender, "task_sent", {"to": task.recipient, **vars(task)})
        agent = self.agents.get(task.recipient)
        if agent is None:
            res = Result(task.id, "bus", "failed", f"unknown agent {task.recipient}")
        else:
            res = agent.run(task)
        self.logger.log(res.sender, "result_returned", {"to": task.sender, **vars(res)})
        return res
