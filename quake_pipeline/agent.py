"""The pipeline: one fixed graph. The order is set here, not by the model."""

from google.adk import Workflow

from .agents import plan_agent, summary_agent
from .nodes import ingest, publish, transform

root_agent = Workflow(
    name="quake_pipeline",
    edges=[("START", plan_agent, ingest, transform, summary_agent, publish)],
)
