import logging
import os
import re
import json
import time
import asyncio
import threading
import queue
import random
import string
from datetime import datetime
from dotenv import load_dotenv
import aiohttp
from typing import Annotated, Dict, List, Optional, Any
from pathlib import Path
from livekit.agents import (
    AutoSubscribe,
    JobContext,
    JobProcess,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.pipeline import AgentCallContext, VoicePipelineAgent
from livekit.plugins import openai, silero, turn_detector
from livekit.plugins.openai.tts import TTS
from firecrawl import FirecrawlApp
load_dotenv(dotenv_path=".env.local")

logger = logging.getLogger("voice-agent")
logger.setLevel(logging.DEBUG)


RESEARCH_DIR = Path("research_results")
os.makedirs(RESEARCH_DIR, exist_ok=True)


active_research_jobs = {}


research_results_cache = {}


research_job_queue = queue.Queue()


def generate_readable_job_id(query, length=4):
    """
    Generate a human-readable job ID based on the query content.
    
    Args:
        query (str): The research query
        length (int): Length of the random string suffix
        
    Returns:
        str: A human-readable job ID
    """

    cleaned_query = re.sub(r'[^\w\s]', '', query.lower())
    words = cleaned_query.split()
    

    common_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'with', 'about', 'is', 'are', 'what', 'why', 'how', 'when', 'where'}
    keywords = [word for word in words if word not in common_words]
    

    keyword_part = ""
    for word in keywords[:2]:
        keyword_part += word[:5] + "_"
    
    if not keyword_part:
        keyword_part = "research_"  
    

    random_string = ''.join(random.choice(string.ascii_lowercase) for _ in range(length))
    

    date_str = datetime.now().strftime("%m%d")
    

    job_id = f"{keyword_part}{random_string}_{date_str}"
    
    return job_id


def save_research_to_file(job_id, research_data):
    """
    Save research results to a JSON file for easier retrieval later.
    
    Args:
        job_id (str): The ID of the research job
        research_data (dict): The research results data
    """
    try:

        filename = RESEARCH_DIR / f"{job_id}.json"
        

        data_to_save = {
            "job_id": job_id,
            "query": research_data.get("query", ""),
            "status": research_data.get("status", "unknown"),
            "timestamp": time.time(),
            "final_analysis": research_data.get("final_analysis", ""),
            "activities": research_data.get("activities", []),
            "error": research_data.get("error", ""),
            "api_job_id": research_data.get("api_job_id", "")
        }
        

        if "results" in research_data and isinstance(research_data["results"], dict):
            if "data" in research_data["results"] and isinstance(research_data["results"]["data"], dict):
                data_to_save["sources"] = research_data["results"]["data"].get("sources", [])
        

        if "sources" in research_data:
            data_to_save["sources"] = research_data["sources"]
        

        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        
     
        research_results_cache[job_id] = data_to_save
        research_results_cache[data_to_save["query"].lower()] = data_to_save
        
        logger.info(f"Saved research results to file: {filename}")
        return str(filename)
    except Exception as e:
        logger.error(f"Error saving research to file: {e}")
        return None


def load_research_from_file(job_id):
    """
    Load research results from a JSON file.
    
    Args:
        job_id (str): The ID of the research job
        
    Returns:
        dict: The research results data, or None if not found
    """
    
    if job_id in research_results_cache:
        logger.info(f"Retrieved research results from cache for job ID: {job_id}")
        return research_results_cache[job_id]
        
    try:
        
        filename = RESEARCH_DIR / f"{job_id}.json"
        
        
        if not filename.exists():
            logger.warning(f"Research file not found: {filename}")
            return None
        
        
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        
        research_results_cache[job_id] = data
        research_results_cache[data["query"].lower()] = data
        
        logger.info(f"Loaded research results from file: {filename}")
        return data
    except Exception as e:
        logger.error(f"Error loading research from file: {e}")
        return None


def research_worker_thread():
    """
    Worker thread function that processes research jobs from the queue.
    This runs in a completely separate thread from the main event loop.
    """
    import asyncio
    
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    while True:
        try:
            
            job = research_job_queue.get()
            if job is None:  
                break
                
            
            job_id = job['job_id']
            query = job['query']
            params = job['params']
            firecrawl_client = job['firecrawl_client']
            
            
            logger.info(f"Thread worker: Starting research for job {job_id}")
            
            
            async def process_job():
                try:
                    
                    async def activity_handler(activity):
                        activity_type = activity.get('type', 'unknown')
                        message = activity.get('message', '')
                        logger.info(f"Job {job_id} activity: {activity_type} - {message}")
                        
                        
                        if job_id in active_research_jobs:
                            if "activities" not in active_research_jobs[job_id]:
                                active_research_jobs[job_id]["activities"] = []
                            active_research_jobs[job_id]["activities"].append(activity)
                    
                    
                    active_research_jobs[job_id]["status"] = "in_progress"
                    
                    try:
                        
                        result_or_coroutine = firecrawl_client.deep_research(
                            query=query,
                            params=params,
                            on_activity=activity_handler
                        )
                        
                        
                        if asyncio.iscoroutine(result_or_coroutine):
                            results = await result_or_coroutine
                        else:
                            
                            results = result_or_coroutine
                        
                        
                        if isinstance(results, dict) and "job_id" in results:
                            api_job_id = results["job_id"]
                            active_research_jobs[job_id]["api_job_id"] = api_job_id
                            
                            
                            active_research_jobs[api_job_id] = active_research_jobs[job_id]
                            
                            logger.info(f"Stored API job ID {api_job_id} for local job {job_id}")
                        
                        
                        active_research_jobs[job_id]["results"] = results
                        active_research_jobs[job_id]["status"] = "completed"
                        
                       
                        if isinstance(results, dict):
                            
                            if "data" in results and isinstance(results["data"], dict):
                                final_analysis = results["data"].get("finalAnalysis", "")
                                if final_analysis:
                                    active_research_jobs[job_id]["final_analysis"] = final_analysis
                                
                                
                                sources = results["data"].get("sources", [])
                                if sources:
                                    active_research_jobs[job_id]["sources"] = sources
                        
                        
                        if "final_analysis" not in active_research_jobs[job_id] and "activities" in active_research_jobs[job_id]:
                            activities = active_research_jobs[job_id]["activities"]
                            synthesis_activities = [a for a in activities if a.get("type") == "synthesis"]
                            if synthesis_activities:
                                final_message = synthesis_activities[-1].get("message", "")
                                if final_message:
                                    active_research_jobs[job_id]["final_analysis"] = final_message
                        
                        
                        save_research_to_file(job_id, active_research_jobs[job_id])
                        
                        logger.info(f"Thread worker: Completed research for job {job_id}")
                        
                    except Exception as e:
                        error_msg = f"Error in deep_research call: {str(e)}"
                        logger.error(error_msg)
                        if job_id in active_research_jobs:
                            active_research_jobs[job_id]["status"] = "error"
                            active_research_jobs[job_id]["error"] = str(e)
                            
                            
                            save_research_to_file(job_id, active_research_jobs[job_id])
                            
                        raise  
                            
                except Exception as e:
                    
                    error_msg = f"Error in worker thread: {str(e)}"
                    logger.error(error_msg)
                    if job_id in active_research_jobs:
                        active_research_jobs[job_id]["status"] = "error"
                        active_research_jobs[job_id]["error"] = str(e)
                        
                       
                        save_research_to_file(job_id, active_research_jobs[job_id])
            
            
            loop.run_until_complete(process_job())
            
        except Exception as e:
            logger.error(f"Error in worker thread: {str(e)}")
        finally:
            
            research_job_queue.task_done()
    
    
    loop.close()
    logger.info("Research worker thread shutting down")

# Start the worker thread
research_thread = threading.Thread(target=research_worker_thread, daemon=True)
research_thread.start()

class AsyncFirecrawlWrapper:
    """
    A wrapper for the Firecrawl client that properly handles async callbacks.
    """
    
    def __init__(self, api_key=None):
        """Initialize with the Firecrawl client"""
        
        self.client = FirecrawlApp(api_url=os.environ.get("FIRECRAWL_API_URL"), api_key='firecrawl')
        
    async def deep_research(self, query, params=None, on_activity=None):
        """
        Wrapper around Firecrawl's deep_research that properly handles async callbacks.
        
        Args:
            query (str): The research query
            params (dict, optional): Research parameters
            on_activity (callable, optional): Async callback function for activity updates
        
        Returns:
            dict: The research results
        """
        
        if on_activity and asyncio.iscoroutinefunction(on_activity):
            
            def sync_activity_handler(activity):
                
                asyncio.create_task(self._safe_call_async(on_activity, activity))
                
            
            try:
               
                result = self.client.deep_research(
                    query=query,
                    params=params,
                    on_activity=sync_activity_handler
                )
                
                
                return result
                
            except Exception as e:
                logger.error(f"Error calling Firecrawl deep_research: {str(e)}")
                raise
        else:
            
            try:
                
                return self.client.deep_research(
                    query=query,
                    params=params,
                    on_activity=on_activity
                )
            except Exception as e:
                logger.error(f"Error calling Firecrawl deep_research: {str(e)}")
                raise
        
    async def check_deep_research_status(self, job_id):
        """Pass-through to the client's method"""
        try:
            
            result = self.client.check_deep_research_status(job_id)
            
            
            if asyncio.iscoroutine(result):
                return await result
            
            return result
            
        except Exception as e:
            logger.error(f"Error checking research status: {str(e)}")
            raise
    
    async def _safe_call_async(self, coro_func, *args, **kwargs):
        """Safely call an async function and log any errors"""
        try:
            await coro_func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in async callback: {str(e)}")


class AssistantFnc(llm.FunctionContext):
    """
    Defines a set of functions that the assistant can execute.
    Includes internet search and deep research capabilities.
    """
    
    # Class-level research tracking to help with status checks
    last_query = None
    last_job_id = None

    def __init__(self):
        """Initialize the assistant functions with Firecrawl client"""
        super().__init__()
        
        api_key = os.environ.get("FIRECRAWL_API_KEY")
        if not api_key:
            logger.warning("FIRECRAWL_API_KEY not found in environment variables")
        self.firecrawl = AsyncFirecrawlWrapper(api_key='firecrawl')

    def _format_for_speech(self, message: str, sources: list = None) -> str:
        """
        Formats results in a natural, conversational way suitable for TTS.
        More thoroughly removes asterisks and other special characters.
        """
        
        clean_message = re.sub(r'\*+', '', message)
        clean_message = re.sub(r'[\[\]\(\)\{\}]', '', clean_message)
        clean_message = re.sub(r'^#+\s+', '', clean_message, flags=re.MULTILINE)
        clean_message = re.sub(r'^\s*[\*\-]\s+', '', clean_message, flags=re.MULTILINE)
        clean_message = re.sub(r'\s+', ' ', clean_message)
        clean_message = re.sub(r'\n+', '\n', clean_message)
        
        
        speech_text = clean_message
        
        
        if sources:
            speech_text += "\n\nThis information comes from "
            source_titles = []
            for source in sources[:2]:  
                title = source.get("title", "")
                if title:
                    
                    clean_title = re.sub(r'\*+', '', title)
                    clean_title = re.sub(r'[\[\]\(\)\{\}]', '', clean_title)
                    clean_title = re.sub(r'\s+', ' ', clean_title)
                    source_titles.append(clean_title)
            
            if source_titles:
                if len(source_titles) == 1:
                    speech_text += f"an article titled {source_titles[0]}"
                else:
                    speech_text += f"articles including {source_titles[0]} and {source_titles[1]}"
                
                if len(sources) > 2:
                    speech_text += f", and {len(sources) - 2} other sources"
        
        return speech_text

    @llm.ai_callable()
    async def internet_search(
        self,
        query: Annotated[
            str,
            llm.TypeInfo(
                description="The search query for performing an internet search using the Perplexica API."
            ),
        ],
    ):
        """
        Performs an internet search using a local Perplexica instance and formats results
        for natural speech output.
        """
        agent = AgentCallContext.get_current().agent

        
        if not agent.chat_ctx.messages or agent.chat_ctx.messages[-1].role != "assistant":
            filler_message = f"Let me look that up for you."
            logger.info(f"Sending filler message: {filler_message}")
            await agent.say(filler_message, add_to_chat_ctx=True)

        logger.info(f"Performing internet search for query: {query}")

        search_url = os.environ.get("SEARCH_API_URL")
        payload = {
            "chatModel": {
                "provider": "ollama",
                "name": os.environ.get("SEARCH_CHAT_MODEL")
            },
            "embeddingModel": {
                "provider": "ollama",
                "name": os.environ.get("SEARCH_EMBEDDING_MODEL")
            },
            "optimizationMode": "speed",
            "focusMode": "webSearch",
            "query": query,
            "history": []
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(search_url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    message = data.get("message", "")
                    sources = data.get("sources", [])

                    
                    speech_text = self._format_for_speech(message, sources)
                    
                    
                    chat_text = speech_text + "\n\nSources:\n" + "\n".join(
                        f"{i+1}. {source.get('metadata', {}).get('title', 'No Title')} - {source.get('metadata', {}).get('url', '')}"
                        for i, source in enumerate(sources)
                    )

                    logger.info(f"Search results formatted for speech: {speech_text}")
                    return {"query": query, "speech_results": speech_text, "chat_results": chat_text}
                else:
                    error_msg = f"I'm sorry, but I wasn't able to complete the search successfully."
                    logger.error(f"Search failed with status code: {response.status}")
                    return {"query": query, "speech_results": error_msg, "chat_results": error_msg}

    @llm.ai_callable()
    async def deep_research(
        self,
        query: Annotated[
            str,
            llm.TypeInfo(
                description="The research topic or question for deep, comprehensive research using Firecrawl."
            ),
        ],
        max_depth: Annotated[
            int,
            llm.TypeInfo(
                description="Maximum number of research iterations (1-10, default: 5)",
            ),
        ] = 5,
        time_limit: Annotated[
            int,
            llm.TypeInfo(
                description="Time limit in seconds (30-300, default: 180)",
            ),
        ] = 180,
        max_urls: Annotated[
            int,
            llm.TypeInfo(
                description="Maximum number of URLs to analyze (1-20, default: 15)",
            ),
        ] = 15,
    ):
        """
        Initiates deep research on a topic using Firecrawl API.
        This is for complex topics requiring comprehensive analysis across multiple sources.
        Runs in a completely separate thread so the agent can continue conversing.
        """
        agent = AgentCallContext.get_current().agent
        
        
        max_depth = min(max(1, max_depth), 10)  # Ensure within 1-10
        time_limit = min(max(30, time_limit), 300)  # Ensure within 30-300
        max_urls = min(max(1, max_urls), 20)  # Ensure within 1-20

        
        job_id = generate_readable_job_id(query)
        
        
        intro_message = f"I'll start a deep research process on '{query}' in the background. You can continue talking with me while I work on this."
        logger.info(f"Starting deep research '{job_id}' with intro: {intro_message}")
        await agent.say(intro_message, add_to_chat_ctx=True)
        
        
        active_research_jobs[job_id] = {
            "query": query,
            "status": "starting",
            "start_time": time.time()
        }
        
        
        AssistantFnc.last_query = query
        AssistantFnc.last_job_id = job_id
        
        
        research_results_cache[query.lower()] = active_research_jobs[job_id]
        
        
        params = {
            "maxDepth": max_depth,
            "timeLimit": time_limit,
            "maxUrls": max_urls
        }
        
        
        logger.info(f"Queuing research job '{job_id}' for background processing")
        research_job_queue.put({
            'job_id': job_id,
            'query': query,
            'params': params,
            'firecrawl_client': self.firecrawl
        })
        
        
        async def check_completion():
            try:
                
                await asyncio.sleep(10)
                
                
                completed = False
                error_occurred = False
                
                while not completed and not error_occurred:
                    try:
                        
                        if job_id in active_research_jobs:
                            status = active_research_jobs[job_id].get("status", "")
                            
                            
                            if status == "completed":
                                completed = True
                                
                                
                                try:
                                    agent_ctx = AgentCallContext.get_current()
                                    if agent_ctx and agent_ctx.agent:
                                        notification = f"I've completed my research on '{query}'. You can ask me about the results when you're ready."
                                        await agent_ctx.agent.say(notification, add_to_chat_ctx=True, allow_interruptions=True)
                                except Exception as e:
                                    logger.error(f"Error notifying about completion: {str(e)}")
                            
                            
                            elif status == "error":
                                error_occurred = True
                                error = active_research_jobs[job_id].get("error", "unknown error")
                                
                                
                                try:
                                    agent_ctx = AgentCallContext.get_current()
                                    if agent_ctx and agent_ctx.agent:
                                        notification = f"I encountered an issue with your research on '{query}': {error}"
                                        await agent_ctx.agent.say(notification, add_to_chat_ctx=True, allow_interruptions=True)
                                except Exception as e:
                                    logger.error(f"Error notifying about error: {str(e)}")
                    except Exception as e:
                        logger.error(f"Error checking job status: {str(e)}")
                    
                    
                    if not completed and not error_occurred:
                        await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"Error in completion checker: {str(e)}")
        
        
        asyncio.create_task(check_completion())
        
        
        return {
            "job_id": job_id,
            "query": query, 
            "status": "started",
            "speech_results": f"I've started researching this topic in the background. You can continue talking with me, and I'll notify you when it's complete.",
            "chat_results": f"Research '{job_id}' for '{query}' has been started in the background. You can continue our conversation while I work on this. Ask for status updates by mentioning the research topic or asking about '{job_id}'."
        }

    @llm.ai_callable()
    async def check_research_status(
        self,
        job_id: Annotated[
            str,
            llm.TypeInfo(
                description="The ID of the research job to check status for."
            ),
        ] = None,
        topic: Annotated[
            str,
            llm.TypeInfo(
                description="The topic of research to check status for (alternative to job_id)."
            ),
        ] = None,
    ):
        """
        Checks the status of an ongoing or completed deep research job.
        Can check by job_id or by topic keywords.
        """
        agent = AgentCallContext.get_current().agent
        
        
        if topic and topic.lower() in research_results_cache:
            logger.info(f"Found research directly in cache for topic: {topic.lower()}")
            job_id = research_results_cache[topic.lower()].get("job_id")
            
        
        if job_id is None and topic is None:
            if AssistantFnc.last_job_id:
                job_id = AssistantFnc.last_job_id
                logger.info(f"Using most recent job ID: {job_id}")
            elif not active_research_jobs:
                
                try:
                    research_files = list(RESEARCH_DIR.glob("*.json"))
                    if research_files:
                        
                        latest_file = max(research_files, key=lambda p: p.stat().st_mtime)
                        job_id = latest_file.stem  
                        logger.info(f"Found latest research file: {latest_file}")
                    else:
                        await agent.say("I don't have any research jobs to check. Would you like me to start one?", 
                                    add_to_chat_ctx=True)
                        return {
                            "status": "no_jobs",
                            "speech_results": "I don't have any research jobs to check. Would you like me to start one?",
                            "chat_results": "I don't have any research jobs to check. Would you like me to start one?"
                        }
                except Exception as e:
                    logger.error(f"Error finding research files: {e}")
                    await agent.say("I don't have any research jobs to check. Would you like me to start one?", 
                                add_to_chat_ctx=True)
                    return {
                        "status": "no_jobs",
                        "speech_results": "I don't have any research jobs to check. Would you like me to start one?",
                        "chat_results": "I don't have any research jobs to check. Would you like me to start one?"
                    }
            else:
                
                latest_job = None
                latest_time = 0
                for jid, info in active_research_jobs.items():
                    job_time = info.get("start_time", 0)
                    if job_time > latest_time:
                        latest_time = job_time
                        latest_job = jid
                
                if latest_job:
                    job_id = latest_job
                    logger.info(f"Using most recent job ID from memory: {job_id}")
                    await agent.say(f"I'll check on your most recent research.", add_to_chat_ctx=True)
        
        
        elif topic and not job_id:
            topic_lower = topic.lower()
            matching_jobs = []
            
            logger.info(f"Searching for research related to topic: '{topic_lower}'")
            
            
            for jid, info in active_research_jobs.items():
                if isinstance(jid, str) and len(jid) > 30:  
                    continue
                    
                query = info.get("query", "").lower()
                
               
                logger.info(f"Comparing topic '{topic_lower}' with query '{query}'")
                
                
                significant_topic_words = [word for word in topic_lower.split() if len(word) > 3 and word not in {'what', 'when', 'where', 'which', 'how', 'that', 'this', 'these', 'those', 'with', 'from'}]
                significant_query_words = [word for word in query.split() if len(word) > 3 and word not in {'what', 'when', 'where', 'which', 'how', 'that', 'this', 'these', 'those', 'with', 'from'}]
                
                
                word_overlap = any(word in query for word in significant_topic_words)
                reverse_overlap = any(word in topic_lower for word in significant_query_words)
                
                
                if (topic_lower in query or 
                    query in topic_lower or 
                    word_overlap or 
                    reverse_overlap):
                    matching_jobs.append((jid, query, info.get("start_time", 0)))
                    logger.info(f"Found matching job: {jid} for query: '{query}'")
            
            
            try:
                research_files = list(RESEARCH_DIR.glob("*.json"))
                for file in research_files:
                    research_data = load_research_from_file(file.stem)
                    if research_data:
                        query = research_data.get("query", "").lower()
                        
                        
                        logger.info(f"Comparing topic '{topic_lower}' with file query '{query}'")
                        
                        
                        significant_topic_words = [word for word in topic_lower.split() if len(word) > 3 and word not in {'what', 'when', 'where', 'which', 'how', 'that', 'this', 'these', 'those', 'with', 'from'}]
                        significant_query_words = [word for word in query.split() if len(word) > 3 and word not in {'what', 'when', 'where', 'which', 'how', 'that', 'this', 'these', 'those', 'with', 'from'}]
                        
                        word_overlap = any(word in query for word in significant_topic_words)
                        reverse_overlap = any(word in topic_lower for word in significant_query_words)
                        
                        if (topic_lower in query or 
                            query in topic_lower or 
                            word_overlap or 
                            reverse_overlap):
                            
                            if not any(jid == file.stem for jid, _, _ in matching_jobs):
                                matching_jobs.append((file.stem, query, research_data.get("timestamp", 0)))
                                logger.info(f"Found matching file: {file.stem} for query: '{query}'")
            except Exception as e:
                logger.error(f"Error searching files by topic: {e}")
            
            if matching_jobs:
                
                matching_jobs.sort(key=lambda x: x[2], reverse=True)
                job_id = matching_jobs[0][0]
                full_query = matching_jobs[0][1]
                
                if len(matching_jobs) > 1:
                    await agent.say(f"I found multiple research jobs related to '{topic}'. I'll check the most recent one about '{full_query}'.", 
                                add_to_chat_ctx=True)
                else:
                    await agent.say(f"I found a research job about '{full_query}'. Let me check its status.", 
                                add_to_chat_ctx=True)
            else:
                
                research_files = list(RESEARCH_DIR.glob("*.json"))
                if research_files:
                    
                    latest_file = max(research_files, key=lambda p: p.stat().st_mtime)
                    job_id = latest_file.stem
                    research_data = load_research_from_file(job_id)
                    if research_data:
                        query = research_data.get("query", "")
                        logger.info(f"Using latest research file as fallback: {latest_file}")
                        await agent.say(f"I found a recent research job about '{query}'. Let me check its status.", 
                                    add_to_chat_ctx=True)
                else:
                    await agent.say(f"I couldn't find any research about '{topic}'. Would you like me to start a new research task on this topic?", 
                                add_to_chat_ctx=True)
                    return {
                        "status": "not_found",
                        "speech_results": f"I couldn't find any research about '{topic}'. Would you like me to start a new research task on this topic?",
                        "chat_results": f"I couldn't find any research about '{topic}'. Would you like me to start a new research task on this topic?"
                    }
        
        
        research_data = None
        if job_id:
            research_data = load_research_from_file(job_id)
        
        
        if research_data:
            logger.info(f"Found research in file with status: {research_data.get('status')}")
            
            status = research_data.get("status", "unknown")
            query = research_data.get("query", "this topic")
            
            
            if status == "completed":
                final_analysis = research_data.get("final_analysis", "")
                sources = research_data.get("sources", [])
                
                
                if not final_analysis and research_data.get("activities"):
                    activities = research_data.get("activities", [])
                    synthesis_activities = [a for a in activities if a.get("type") == "synthesis"]
                    if synthesis_activities:
                        final_analysis = synthesis_activities[-1].get("message", "")
                
                
                if not final_analysis:
                    final_analysis = "I completed the research but couldn't extract a proper analysis."
                
                
                speech_text = f"Research on '{query}' is complete! "
                speech_text += self._format_for_speech(final_analysis, sources)
                
                
                chat_text = f"# Deep Research Results: {query}\n\n"
                chat_text += final_analysis + "\n\n"
                
                if sources:
                    chat_text += "## Sources:\n" + "\n".join(
                        f"{i+1}. [{source.get('title', 'No Title')}]({source.get('url', '')})"
                        for i, source in enumerate(sources)
                    )
                
                
                await agent.say(speech_text, add_to_chat_ctx=True)
                
                return {
                    "job_id": job_id,
                    "query": query,
                    "status": "completed",
                    "speech_results": speech_text,
                    "chat_results": chat_text
                }
            
            
            elif status == "error":
                query = research_data.get("query", "your topic")
                error = research_data.get("error", "unknown error")
                final_analysis = ""
                
                
                if research_data.get("activities"):
                    activities = research_data.get("activities")
                    synthesis_activities = [a for a in activities if a.get("type") == "synthesis"]
                    if synthesis_activities:
                        final_analysis = synthesis_activities[-1].get("message", "")
                
                
                if final_analysis:
                    speech_text = f"Although there was an error with the research on '{query}', I found some information: {final_analysis}"
                    
                    chat_text = f"# Partial Research Results: {query}\n\n"
                    chat_text += final_analysis + "\n\n"
                    chat_text += f"*Note: The research encountered an error: {error}*"
                    
                    
                    await agent.say(speech_text, add_to_chat_ctx=True)
                    
                    return {
                        "job_id": job_id,
                        "query": query,
                        "status": "partial",
                        "speech_results": speech_text,
                        "chat_results": chat_text
                    }
                else:
                    
                    error_msg = f"I encountered an issue with your research on '{query}': {error}"
                    await agent.say(error_msg, add_to_chat_ctx=True)
                    
                    return {
                        "job_id": job_id,
                        "query": query,
                        "status": "error",
                        "speech_results": error_msg,
                        "chat_results": error_msg
                    }
            
            
            elif status in ["starting", "in_progress"]:
                query = research_data.get("query", "your topic")
                
                
                activities = research_data.get("activities", [])
                
                progress_message = f"Research on '{query}' is currently in progress"
                
                
                if activities:
                    latest_activity = activities[-1]
                    activity_type = latest_activity.get("type", "")
                    activity_msg = latest_activity.get("message", "")
                    
                    if activity_type and activity_msg:
                        progress_message += f". Currently {activity_type}ing {activity_msg}"
                
                progress_message += ". You can continue our conversation and I'll notify you when it's complete."
                
                await agent.say(progress_message, add_to_chat_ctx=True)
                
                return {
                    "job_id": job_id,
                    "query": query,
                    "status": "in_progress",
                    "speech_results": progress_message,
                    "chat_results": progress_message
                }
        
        
        try:
            
            if job_id in active_research_jobs:
                status = active_research_jobs[job_id].get("status", "unknown")
                query = active_research_jobs[job_id].get("query", "your research")
                
                
                progress_message = f"The current status of your deep research on '{query}' is \"{status}\". "
                if status == "in_progress" or status == "starting":
                    progress_message += "It's still working through the analysis process. I'll keep monitoring it, and let you know as soon as it's done."
                
                await agent.say(progress_message, add_to_chat_ctx=True)
                
                return {
                    "job_id": job_id,
                    "query": query,
                    "status": status,
                    "speech_results": progress_message,
                    "chat_results": progress_message
                }
            
            
            api_status = await self.firecrawl.check_deep_research_status(job_id)
            
            
            if isinstance(api_status, dict):
                status = api_status.get("status", "unknown")
                query = None
                
               
                if "data" in api_status and "activities" in api_status["data"]:
                    activities = api_status["data"]["activities"]
                    for activity in activities:
                        message = activity.get("message", "")
                        if "analyzing" in message.lower() or "researching" in message.lower():
                            
                            query = message.replace("Analyzing", "").replace("analyzing", "").replace("Researching", "").replace("researching", "").strip()
                            break
                
                
                if not query:
                    query = "your research topic"
                
                
                if status == "completed":
                    final_analysis = None
                    sources = []
                    
                    if "data" in api_status and isinstance(api_status["data"], dict):
                        final_analysis = api_status["data"].get("finalAnalysis", "")
                        sources = api_status["data"].get("sources", [])
                    
                    if not final_analysis:
                        final_analysis = "I completed the research but couldn't extract a proper analysis."
                    
                    
                    speech_text = f"Research on '{query}' is complete! "
                    speech_text += self._format_for_speech(final_analysis, sources)
                    
                    
                    chat_text = f"# Deep Research Results: {query}\n\n"
                    chat_text += final_analysis + "\n\n"
                    
                    if sources:
                        chat_text += "## Sources:\n" + "\n".join(
                            f"{i+1}. [{source.get('title', 'No Title')}]({source.get('url', '')})"
                            for i, source in enumerate(sources)
                        )
                    
                    
                    job_data = {
                        "job_id": job_id,
                        "query": query,
                        "status": "completed",
                        "final_analysis": final_analysis,
                        "sources": sources,
                        "timestamp": time.time()
                    }
                    active_research_jobs[job_id] = job_data
                    save_research_to_file(job_id, job_data)
                    
                    
                    await agent.say(speech_text, add_to_chat_ctx=True)
                    
                    return {
                        "job_id": job_id,
                        "query": query,
                        "status": "completed",
                        "speech_results": speech_text,
                        "chat_results": chat_text
                    }
                
                
                elif status in ["in_progress", "starting"]:
                    progress_message = f"The current status of your deep research is \"{status}\". It's still working through the analysis process. I'll keep monitoring it, and let you know as soon as it's done."
                    
                    await agent.say(progress_message, add_to_chat_ctx=True)
                    
                    return {
                        "job_id": job_id,
                        "query": query,
                        "status": status,
                        "speech_results": progress_message,
                        "chat_results": progress_message
                    }
                
        except Exception as e:
            logger.error(f"Error checking API status: {e}")
            
        
        
        try:
            research_files = list(RESEARCH_DIR.glob("*.json"))
            if research_files:
                
                latest_file = max(research_files, key=lambda p: p.stat().st_mtime)
                research_data = load_research_from_file(latest_file.stem)
                
                if research_data:
                    query = research_data.get("query", "unknown topic")
                    status = research_data.get("status", "unknown")
                    
                    fallback_msg = f"I found a research on '{query}' with status '{status}'. Let me check its details."
                    await agent.say(fallback_msg, add_to_chat_ctx=True)
                    
                    
                    return await self.check_research_status(job_id=latest_file.stem)
        except Exception as e:
            logger.error(f"Error in fallback file check: {e}")
        
        
        await agent.say("I couldn't find any details for this research. Would you like me to start a new research task?", add_to_chat_ctx=True)
        return {
            "status": "not_found",
            "speech_results": "I couldn't find any details for this research. Would you like me to start a new research task?",
            "chat_results": "I couldn't find any details for this research. Would you like me to start a new research task?"
        }



def prewarm(proc: JobProcess):
    """
    Runs once before the agent starts to load heavy models (e.g., VAD).
    """
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext):
   
    system_prompt_path = Path("system_prompt.txt")
    try:
        with open(system_prompt_path, 'r', encoding='utf-8') as f:
            system_prompt = f.read()
        logger.info(f"Successfully loaded system prompt from {system_prompt_path}")
    except Exception as e:
       
        logger.error(f"Failed to load system prompt from file: {e}")
        system_prompt = "You are  an AI assistant that can help with internet searches and deep research."
        
    
    initial_ctx = llm.ChatContext().append(
        role="system",
        text=system_prompt
    )
    
    logger.info(f"Connecting to room: {ctx.room.name}")
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    
    participant = await ctx.wait_for_participant()
    logger.info(f"Participant joined: {participant.identity}")

    
    stt_plugin = openai.STT.with_faster_whisper(
        model= os.environ.get("STT_MODEL"), 
    )

    
    llm_plugin = openai.LLM.with_ollama(
        base_url= os.environ.get("LLM_BASE_URL"), 
        model= os.environ.get("LLM_MODEL")
    )

    
    tts_plugin = TTS.create_kokoro_client(
        model= os.environ.get("TTS_MODEL"),          
        voice= os.environ.get("TTS_VOICE"),         
        speed= os.environ.get("TTS_SPEED"),
        base_url= os.environ.get("TTS_BASE_URL"), 
        api_key= os.environ.get("TTS_API_KEY"),          
    )

    
    fnc_ctx = AssistantFnc()

   
    agent = VoicePipelineAgent(
        vad=ctx.proc.userdata["vad"],
        stt=stt_plugin,
        llm=llm_plugin,
        tts=tts_plugin,
        fnc_ctx=fnc_ctx,
        chat_ctx=initial_ctx,
        turn_detector=turn_detector.EOUModel(),  
        max_nested_fnc_calls=3,  
    )

    
    async def log_agent_activities(event_type, data):
        if event_type == "speech_start":
            logger.info(f"Agent started speaking: {data.get('text', '')[:30]}...")
        elif event_type == "speech_end":
            logger.info(f"Agent finished speaking")
        elif event_type == "error":
            logger.error(f"Agent error: {data}")
    
    
    agent.start(ctx.room, participant)

    
    await agent.say("Hey, how can I help you today? I can search the web for quick answers or conduct deep research on complex topics.", add_to_chat_ctx=True, allow_interruptions=True)


if __name__ == "__main__":
    
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
        ),
    )
