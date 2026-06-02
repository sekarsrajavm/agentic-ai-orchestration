"""
Agentic AI Multi-Agent Orchestration System
Core orchestrator module for managing autonomous agents
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime
from enum import Enum

import httpx
from azure.identity import DefaultAzureCredential
from openai import AsyncAzureOpenAI

logger = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    """Agent execution status"""
    IDLE = "idle"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    HUMAN_REVIEW = "human_review_required"


class Agent:
    """Autonomous agent with tool access and state management"""
    
    def __init__(
        self,
        name: str,
        role: str,
        tools: List[str],
        model: str = "gpt-4",
        temperature: float = 0.1,
        max_tokens: int = 2000
    ):
        self.name = name
        self.role = role
        self.tools = tools
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.status = AgentStatus.IDLE
        self.execution_history: List[Dict[str, Any]] = []
        
    async def execute(
        self,
        task: str,
        context: Dict[str, Any],
        client: AsyncAzureOpenAI,
        tool_server_url: str
    ) -> Dict[str, Any]:
        """Execute agent task with tool access"""
        
        self.status = AgentStatus.PROCESSING
        start_time = datetime.now()
        
        try:
            # Build system prompt
            system_prompt = f"""You are {self.name}, an AI agent with the following role:
{self.role}

Available tools: {', '.join(self.tools)}

Instructions:
1. Analyze the task carefully
2. Use tools as needed to complete the task
3. Provide clear reasoning for decisions
4. Flag any items requiring human review

Always respond with valid JSON containing:
- "reasoning": Your step-by-step reasoning
- "actions": List of tool calls to make
- "result": Final result or recommendation
- "confidence": Confidence score 0-1
- "requires_human_review": Boolean flag
"""

            # Create message for LLM
            messages = [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": f"Task: {task}\n\nContext: {json.dumps(context)}"
                }
            ]
            
            # Call Azure OpenAI
            response = await client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"}
            )
            
            # Parse response
            result = json.loads(response.choices[0].message.content)
            
            # Execute tool calls if needed
            if result.get("actions"):
                tool_results = await self._execute_tools(
                    result["actions"],
                    tool_server_url
                )
                result["tool_results"] = tool_results
            
            # Update status
            self.status = (
                AgentStatus.HUMAN_REVIEW
                if result.get("requires_human_review")
                else AgentStatus.SUCCESS
            )
            
            # Record execution
            execution_record = {
                "timestamp": datetime.now().isoformat(),
                "task": task,
                "result": result,
                "duration_ms": (datetime.now() - start_time).total_seconds() * 1000,
                "tokens_used": response.usage.total_tokens if response.usage else 0
            }
            self.execution_history.append(execution_record)
            
            return execution_record
            
        except Exception as e:
            logger.error(f"Agent {self.name} execution failed: {str(e)}")
            self.status = AgentStatus.FAILED
            return {
                "timestamp": datetime.now().isoformat(),
                "task": task,
                "error": str(e),
                "status": "failed"
            }
    
    async def _execute_tools(
        self,
        actions: List[Dict[str, Any]],
        tool_server_url: str
    ) -> List[Dict[str, Any]]:
        """Execute tool calls via centralized tool server"""
        
        results = []
        async with httpx.AsyncClient() as client:
            for action in actions:
                try:
                    response = await client.post(
                        f"{tool_server_url}/execute",
                        json={
                            "tool_name": action.get("tool"),
                            "parameters": action.get("parameters", {})
                        },
                        timeout=30.0
                    )
                    results.append({
                        "tool": action.get("tool"),
                        "status": "success",
                        "result": response.json()
                    })
                except Exception as e:
                    logger.error(f"Tool execution failed: {str(e)}")
                    results.append({
                        "tool": action.get("tool"),
                        "status": "failed",
                        "error": str(e)
                    })
        
        return results


class AgentOrchestrator:
    """Orchestrates multiple agents for complex workflows"""
    
    def __init__(
        self,
        model: str = "gpt-4",
        temperature: float = 0.1,
        max_iterations: int = 10,
        tool_server_url: str = "http://localhost:8000"
    ):
        self.model = model
        self.temperature = temperature
        self.max_iterations = max_iterations
        self.tool_server_url = tool_server_url
        self.agents: Dict[str, Agent] = {}
        
        # Initialize Azure OpenAI client
        self.client = AsyncAzureOpenAI(
            api_version="2024-02-15-preview"
        )
        
    def add_agent(self, agent: Agent) -> None:
        """Register an agent with the orchestrator"""
        self.agents[agent.name] = agent
        logger.info(f"Agent registered: {agent.name}")
    
    async def run(
        self,
        task: str,
        context: Optional[Dict[str, Any]] = None,
        timeout: int = 300
    ) -> Dict[str, Any]:
        """Execute workflow with all registered agents"""
        
        start_time = datetime.now()
        context = context or {}
        execution_log = []
        
        try:
            # Execute agents sequentially
            for agent_name, agent in self.agents.items():
                logger.info(f"Executing agent: {agent_name}")
                
                result = await asyncio.wait_for(
                    agent.execute(task, context, self.client, self.tool_server_url),
                    timeout=timeout
                )
                
                execution_log.append(result)
                
                # Update context with agent results
                if result.get("result"):
                    context[agent_name] = result["result"]
            
            # Aggregate results
            final_result = {
                "status": "success",
                "task": task,
                "execution_log": execution_log,
                "total_duration_ms": (datetime.now() - start_time).total_seconds() * 1000,
                "agents_executed": len(self.agents),
                "confidence": self._calculate_confidence(execution_log)
            }
            
            # Check if human review needed
            if any(log.get("result", {}).get("requires_human_review") for log in execution_log):
                final_result["status"] = "human_review_required"
            
            return final_result
            
        except asyncio.TimeoutError:
            logger.error("Workflow execution timeout")
            return {
                "status": "failed",
                "error": "Execution timeout",
                "execution_log": execution_log
            }
        except Exception as e:
            logger.error(f"Workflow execution failed: {str(e)}")
            return {
                "status": "failed",
                "error": str(e),
                "execution_log": execution_log
            }
    
    def _calculate_confidence(self, execution_log: List[Dict[str, Any]]) -> float:
        """Calculate overall confidence score"""
        if not execution_log:
            return 0.0
        
        confidences = [
            log.get("result", {}).get("confidence", 0.5)
            for log in execution_log
            if log.get("result")
        ]
        
        return sum(confidences) / len(confidences) if confidences else 0.0
    
    def get_agent_stats(self) -> Dict[str, Any]:
        """Get statistics for all agents"""
        return {
            agent_name: {
                "status": agent.status.value,
                "executions": len(agent.execution_history),
                "last_execution": agent.execution_history[-1] if agent.execution_history else None
            }
            for agent_name, agent in self.agents.items()
        }


# Example usage
if __name__ == "__main__":
    import asyncio
    
    async def main():
        # Initialize orchestrator
        orchestrator = AgentOrchestrator(model="gpt-4")
        
        # Create agents
        enrollment_agent = Agent(
            name="enrollment_processor",
            role="Process insurance enrollments and validate documents",
            tools=["document_analyzer", "policy_validator", "enrollment_logger"]
        )
        
        verification_agent = Agent(
            name="verification_processor",
            role="Verify enrollment documents and detect signatures",
            tools=["signature_detector", "stamp_detector", "audit_logger"]
        )
        
        # Add agents
        orchestrator.add_agent(enrollment_agent)
        orchestrator.add_agent(verification_agent)
        
        # Execute workflow
        result = await orchestrator.run(
            task="Process and verify enrollment document",
            context={"document_id": "ENR-2025-001"}
        )
        
        print(json.dumps(result, indent=2))
    
    asyncio.run(main())
