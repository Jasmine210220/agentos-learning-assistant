from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskInput(Strict):
    repository: str = Field(default='', max_length=300)
    goal: str = Field(default="分析项目结构和核心调用流程，生成有代码证据的报告与流程图。", min_length=3, max_length=2000)
    ref: str = Field(default="", max_length=120)
    kind: Literal['analyze', 'learn', 'search', 'chart', 'debug', 'local', 'improve'] = 'analyze'
    learner_background: str = Field(default='具备基础编程知识；具体基础未提供', max_length=1000)
    input_text: str = Field(default='', max_length=160_000)
    attachment_id: str = Field(default='', max_length=32)
    diagram_kind: Literal['flow', 'architecture', 'both'] = 'flow'
    source_task_id: str = Field(default='', pattern=r'^(?:[a-f0-9]{32})?$')


class Evidence(Strict):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class Claim(Strict):
    title: str
    explanation: str = Field(max_length=600)
    evidence: list[Evidence] = Field(min_length=1)
    inferred: bool = False


class Analysis(Strict):
    summary: str
    stack: list[str]
    modules: list[Claim] = Field(min_length=1, max_length=6)
    flow: list[Claim] = Field(min_length=1, max_length=8)
    limitations: list[str]
    risks: list['BugFinding'] = Field(default_factory=list, max_length=8)


class BugFinding(Strict):
    title: str
    severity: Literal['high', 'medium', 'low']
    trigger: str
    cause: str
    impact: str
    suggestion: str
    evidence: list[Evidence] = Field(min_length=1)
    verification: Literal['静态推测', '待验证风险'] = '静态推测'
    patch: str = ''


class LearningAnalysis(Analysis):
    scope_complete: bool
    scope_reason: str = Field(min_length=1, max_length=600)
    technologies: list[Claim] = Field(min_length=1, max_length=6)
    goal_answers: list[Claim] = Field(default_factory=list, max_length=4)


class ScopeAssessment(Strict):
    scope_complete: bool
    reason: str = Field(min_length=1, max_length=600)
    evidence: list[Evidence] = Field(min_length=1, max_length=6)


class ReadingStep(Strict):
    title: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=400)
    module_index: int = Field(ge=1, strict=True)
    evidence: list[Evidence] = Field(min_length=1, max_length=6)
    concepts: list[str] = Field(min_length=1, max_length=4)


class Exercise(Strict):
    title: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=400)
    instructions: list[str] = Field(min_length=1, max_length=6)
    acceptance: list[str] = Field(min_length=1, max_length=6)
    evidence: list[Evidence] = Field(min_length=1, max_length=6)


class LearningPlan(Strict):
    background: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=400)
    flow_indices: list[int] = Field(min_length=1, max_length=8)
    reading_order: list[ReadingStep] = Field(min_length=1, max_length=8)
    exercises: list[Exercise] = Field(min_length=3, max_length=3)
    limitations: list[str]


class BugReport(Strict):
    summary: str
    findings: list[BugFinding] = Field(max_length=8)
    limitations: list[str]


class Plan(Strict):
    objective: str
    focus: list[str] = Field(min_length=1, max_length=6)
    acceptance: list[str] = Field(min_length=1, max_length=6)


class FileSelection(Strict):
    paths: list[str] = Field(min_length=1,max_length=24)
    reason: str


class DiagramNode(Strict):
    id: str = Field(pattern=r"^n[0-9]{1,3}$")
    label: str = Field(min_length=1, max_length=100)
    evidence: list[Evidence] = Field(min_length=1)


class DiagramEdge(Strict):
    source: str
    target: str
    label: str = Field(max_length=100)
    inferred: bool
    evidence: list[Evidence] = Field(min_length=1)


class Diagram(Strict):
    title: str
    nodes: list[DiagramNode] = Field(min_length=2, max_length=16)
    edges: list[DiagramEdge] = Field(min_length=1, max_length=24)


class Review(Strict):
    passed: bool
    summary: str
    issues: list[str]
    repair_targets: list[Literal['code','flow','architecture']] = Field(default_factory=lambda:['code','flow','architecture'])


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "needs_attention"}
ROLES = ["Supervisor", "Code Agent", "Chart Agent", "Reviewer Agent"]
