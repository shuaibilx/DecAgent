"""Exercise documented installation without credentials or a database."""

import json
import subprocess
import sys

import fastapi
import langchain_openai
import langgraph.graph
import psycopg

import decagent

if __name__ == "__main__":
    for args in [
        ["-m", "pip", "check"],
        ["-m", "decagent", "--help"],
        ["-m", "decagent", "doctor"],
    ]:
        subprocess.run([sys.executable, *args], check=True)
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "decagent": decagent.__version__,
                "fastapi": fastapi.__version__,
                "psycopg": psycopg.__version__,
                "langgraph_import": bool(langgraph.graph.StateGraph),
                "model_adapter_import": bool(langchain_openai.ChatOpenAI),
            }
        )
    )
