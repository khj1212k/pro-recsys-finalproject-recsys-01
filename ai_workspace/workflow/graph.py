"""
LangGraph Graph Construction
Defines the workflow graph with nodes and edges
"""
from langgraph.graph import StateGraph, END

from workflow.state import AgentState
from workflow.nodes import (
    initialize_cluster_processing,
    evaluate_cluster,
    handle_cluster_eval_failure,
    generate_newsletter,
    evaluate_newsletter,
    save_newsletter_to_db,
    handle_newsletter_max_retries,
    route_after_cluster_eval,
    route_after_newsletter_eval,
    route_after_cluster_fail
)


def create_newsletter_workflow() -> StateGraph:
    """
    Create the LangGraph workflow for newsletter generation (Single Cluster).
    
    Flow:
    1. Initialize cluster processing
    2. Evaluate cluster coherence
       - PASS -> Generate newsletter
       - FAIL -> End
    3. Generate newsletter
    4. Evaluate newsletter quality
       - PASS -> Save -> End
       - FAIL -> Retry -> Generate
       - Max Retries -> End
    """
    
    # Create the graph
    workflow = StateGraph(AgentState)
    
    # Add all nodes
    workflow.add_node("init_cluster", initialize_cluster_processing)
    workflow.add_node("eval_cluster", evaluate_cluster)
    workflow.add_node("handle_cluster_fail", handle_cluster_eval_failure)
    workflow.add_node("generate_newsletter", generate_newsletter)
    workflow.add_node("eval_newsletter", evaluate_newsletter)
    workflow.add_node("save_newsletter", save_newsletter_to_db)
    workflow.add_node("handle_max_retries", handle_newsletter_max_retries)
    
    # Set entry point
    workflow.set_entry_point("init_cluster")
    
    # Add edges
    
    # Init -> Eval (Direct)
    workflow.add_edge("init_cluster", "eval_cluster")
    
    # After cluster eval: route based on decision
    workflow.add_conditional_edges(
        "eval_cluster",
        route_after_cluster_eval,
        {
            "pass": "generate_newsletter",
            "fail": "handle_cluster_fail"
        }
    )
    
    # After cluster fail handling: decide to retry or end
    workflow.add_conditional_edges(
        "handle_cluster_fail",
        route_after_cluster_fail,
        {
            "retry": "eval_cluster",
            "end": END
        }
    )
    
    # After newsletter generation: evaluate it
    workflow.add_edge("generate_newsletter", "eval_newsletter")
    
    # After newsletter eval: route based on decision
    workflow.add_conditional_edges(
        "eval_newsletter",
        route_after_newsletter_eval,
        {
            "pass": "save_newsletter",
            "retry": "generate_newsletter",
            "max_retries": "handle_max_retries"
        }
    )
    
    # Save -> END (No loop back)
    workflow.add_edge("save_newsletter", END)
    
    # Max retries -> END (No loop back)
    workflow.add_edge("handle_max_retries", END)
    
    return workflow


def compile_workflow():
    """Compile and return the workflow"""
    workflow = create_newsletter_workflow()
    return workflow.compile()
