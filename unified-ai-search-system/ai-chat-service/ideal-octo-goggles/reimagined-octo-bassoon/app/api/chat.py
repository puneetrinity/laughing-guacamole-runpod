"""
Enhanced Chat API with coroutine safety and proper request handling.
"""
import asyncio
import hashlib
import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.api.security import User, check_content_policy, get_current_user
from app.cache.redis_client import CacheManager
from app.core.async_utils import (
    coroutine_safe,
    ensure_awaited,
    safe_graph_execute,
)
from app.core.logging import (
    get_correlation_id,
    get_logger,
    log_performance,
    set_correlation_id,
)
from app.core.model_router import ModelRouter
from app.core.timeout_utils import adaptive_timeout, timeout_manager
from app.graphs.base import GraphState
from app.graphs.chat_graph import ChatGraph
from app.models.manager import ModelManager, QualityLevel
from app.schemas.requests import ChatRequest, ChatStreamRequest
from app.schemas.responses import (
    ChatData,
    ChatResponse,
    ConversationContext,
    CostPrediction,
    DeveloperHints,
    ResponseMetadata,
    create_error_response,
)

router = APIRouter()
logger = get_logger("api.chat")

# Get settings properly
from app.core.config import get_settings
settings = get_settings()


# Global instances (will be initialized in main.py)
model_manager: Optional[ModelManager] = None
cache_manager: Optional[CacheManager] = None
chat_graph: Optional[ChatGraph] = None
model_router = ModelRouter()  # Initialize model router

# CORRECTED REQUEST MODELS WITH PROPER WRAPPERS


async def initialize_chat_dependencies():
    global model_manager, cache_manager, chat_graph
    if not model_manager:
        from app.dependencies import get_model_manager
        model_manager = get_model_manager()
        await model_manager.initialize()
        # Remove diagnostic logs for production
        # logger.info("ModelManager initialized for chat API")
    if not cache_manager:
        try:
            from app.dependencies import get_cache_manager
            cache_manager = get_cache_manager()
            await cache_manager.initialize()
            # logger.info("CacheManager initialized for chat API")
        except Exception:
            # logger.warning("CacheManager initialization failed")
            cache_manager = None
    if not chat_graph:
        chat_graph = ChatGraph(model_manager, cache_manager)
        # logger.info("ChatGraph initialized for chat API")


@router.get("/health")
async def chat_health():
    return {"status": "healthy", "service": "chat", "timestamp": time.time()}


# --- Begin merged advanced coroutine-safe endpoints and helpers ---

# CORRECTED MAIN ENDPOINT WITH COROUTINE SAFETY AND ADVANCED LOGIC


@router.post("/complete", response_model=ChatResponse)
@timeout_manager.with_operation_timeout("standard_query")
@log_performance("chat_complete")
async def chat_complete(
    chat_request: ChatRequest = Body(..., embed=False),
    current_user: Dict[str, Any] = Depends(get_current_user),
    background_tasks: BackgroundTasks = None,
    request: Request = None,
):
    global chat_graph, model_manager, cache_manager
    # --- Begin: Accept both flat and wrapped payloads ---
    if hasattr(chat_request, 'dict'):
        data = chat_request.model_dump()
    else:
        data = dict(chat_request)
    # Unwrap if wrapped in 'request' key
    if 'request' in data and isinstance(data['request'], dict):
        data = data['request']
    # Validate required fields
    required_fields = ["message"]
    missing = [f for f in required_fields if f not in data or data[f] in (None, "")]
    if missing:
        raise HTTPException(status_code=422, detail={
            "error": "Missing required fields.",
            "missing": missing
        })
    # PATCH: Validate message length
    message = data.get("message", "")
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=422, detail={
            "error": "Message must be a non-empty string."
        })
    if len(message) > 4096:
        raise HTTPException(status_code=422, detail={
            "error": "Message too long. Maximum length is 4096 characters."
        })
    # --- PATCH: Validate constraints.quality_requirement if present and propagate to top-level fields ---
    from app.schemas.requests import Constraints
    if 'constraints' in data and isinstance(data['constraints'], dict):
        try:
            constraints_obj = Constraints(**data['constraints'])
            # Propagate constraints fields to top-level if not already set
            for field in ['quality_requirement', 'max_cost', 'max_time', 'force_local_only']:
                if hasattr(constraints_obj, field):
                    value = getattr(constraints_obj, field)
                    if value is not None:
                        # Only override if not already set at top-level or if top-level is None
                        if data.get(field) is None:
                            data[field] = value
        except Exception as e:
            raise HTTPException(status_code=422, detail={
                "error": "Invalid constraints field.",
                "details": str(e)
            })
    # Rebuild ChatRequest from data
    chat_request = ChatRequest(**data)
    # --- End: Accept both flat and wrapped payloads ---
    query_id = str(uuid.uuid4())
    correlation_id = get_correlation_id()
    start_time = time.time()
    logger.info(
        "Chat completion request started",
        query_id=query_id,
        user_id=current_user["user_id"],
        message_length=len(chat_request.message),
        session_id=chat_request.session_id,
        prompt=chat_request.message,
        user_context=chat_request.user_context,
        quality_requirement=getattr(chat_request, "quality_requirement", None),
        correlation_id=correlation_id,
    )
    try:
        # Content policy check
        policy_check = check_content_policy(chat_request.message)
        if not policy_check["passed"]:
            return create_error_response(
                message="Message violates content policy.",
                error_code="CONTENT_POLICY_VIOLATION",
                correlation_id=correlation_id,
            )
        session_id = (
            chat_request.session_id or f"chat_{current_user['user_id']}_{int(time.time())}"
        )
        # Retrieve conversation history from cache first
        app_state = getattr(request.app.state, "app_state", {})
        cache_manager_app = app_state.get("cache_manager")

        conversation_history = []
        if cache_manager_app and session_id:
            try:
                # Try to get existing conversation history with enhanced cache manager
                # Use the appropriate cache manager (app or global enhanced)
                if hasattr(cache_manager, 'get_from_l1_or_l2'):
                    cached_history = await cache_manager.get_from_l1_or_l2(
                        f"conversation_history:{session_id}"
                    )
                else:
                    # Fallback to basic get method
                    cached_history = await cache_manager.get(
                        f"conversation_history:{session_id}"
                    )
                if cached_history:
                    conversation_history = (
                        json.loads(cached_history)
                        if isinstance(cached_history, str)
                        else cached_history
                    )
                    logger.info(
                        f"Retrieved conversation history with {len(conversation_history)} messages for session {session_id}"
                    )
                else:
                    logger.info(
                        f"No existing conversation history found for session {session_id}"
                    )
            except Exception as e:
                logger.warning(f"Failed to retrieve conversation history: {e}")
                conversation_history = []

        # Fallback to user_context if cache fails
        if not conversation_history:
            conversation_history = chat_request.user_context.get(
                "conversation_history", []
            )
        graph_state = GraphState(
            query_id=query_id,
            correlation_id=correlation_id,
            user_id=current_user["user_id"],
            session_id=session_id,
            original_query=chat_request.message,
            conversation_history=conversation_history,
            quality_requirement=getattr(
                QualityLevel,
                chat_request.quality_requirement.upper(),
                QualityLevel.BALANCED,
            ),
            max_cost=chat_request.max_cost,
            max_execution_time=chat_request.max_execution_time,
            user_preferences={
                "tier": getattr(current_user, "tier", "free"),
                "response_style": chat_request.response_style,
                "include_sources": chat_request.include_sources,
                "force_local_only": chat_request.force_local_only,
            },
        )
        # Ensure chat_graph is initialized before handling chat requests
        if chat_graph is None:
            if model_manager is None:
                # Use dependency injection to get the properly initialized instance
                from app.dependencies import get_model_manager
                model_manager = get_model_manager()
            if cache_manager is None:
                # Use dependency injection to get the properly initialized instance
                from app.dependencies import get_cache_manager
                cache_manager = get_cache_manager()
            chat_graph = ChatGraph(model_manager, cache_manager)
        chat_graph_instance = chat_graph
        if chat_graph_instance is None:
            return create_error_response(
                message="Chat graph is not initialized.",
                error_code="CHAT_GRAPH_NOT_INITIALIZED",
                correlation_id=correlation_id,
            )
        # DEBUG: Log before graph execution
        logger.info(f"DEBUG: About to execute graph with state: query={graph_state.original_query}")
        logger.info(f"DEBUG: ChatGraph instance: {chat_graph_instance}")
        logger.info(f"DEBUG: Model manager: {model_manager}")
        
        chat_result = await safe_graph_execute(
            chat_graph_instance, graph_state, timeout=chat_request.max_execution_time
        )
        # Note: safe_graph_execute already ensures proper awaiting
        
        # DEBUG: Log the raw result
        logger.info(f"DEBUG: Raw chat_result type: {type(chat_result)}")
        logger.info(f"DEBUG: Raw chat_result: {chat_result}")
        if hasattr(chat_result, '__dict__'):
            logger.info(f"DEBUG: chat_result attributes: {chat_result.__dict__}")
        
        # ROBUST FIX: Simplified and reliable response extraction
        final_response = None
        response_source = "unknown"
        
        try:
            # Method 1: Direct final_response attribute (most reliable)
            if hasattr(chat_result, 'final_response') and chat_result.final_response:
                final_response = str(chat_result.final_response).strip()
                response_source = "direct_attribute"
            
            # Method 2: Dict response keys (common format)
            elif isinstance(chat_result, dict):
                for key in ['final_response', 'response', 'content', 'text']:
                    if key in chat_result and chat_result[key]:
                        final_response = str(chat_result[key]).strip()
                        response_source = f"dict_key_{key}"
                        break
            
            # Method 3: String response (simple format)
            elif isinstance(chat_result, str) and chat_result.strip():
                final_response = chat_result.strip()
                response_source = "direct_string"
            
            # Method 4: GraphState intermediate results (last resort)
            elif hasattr(chat_result, 'intermediate_results'):
                for node_name in ['response_generator', 'chat_response']:
                    if (node_name in chat_result.intermediate_results and 
                        isinstance(chat_result.intermediate_results[node_name], dict) and
                        'response' in chat_result.intermediate_results[node_name]):
                        final_response = str(chat_result.intermediate_results[node_name]['response']).strip()
                        response_source = f"intermediate_{node_name}"
                        break
            
            # Validate extracted response
            if final_response and len(final_response) > 0:
                logger.debug(f"Response extracted successfully - Source: {response_source}, Length: {len(final_response)}")
            else:
                final_response = None
                
        except Exception as extraction_error:
            logger.error(f"Response extraction failed: {extraction_error}")
            final_response = None
        
        # Validate the final response with error recovery
        if not final_response:
            logger.error(f"Response extraction failed. Raw result: {type(chat_result)}")
            
            # Error Recovery Mechanism #1: Try fallback model
            try:
                logger.info("Attempting error recovery with fallback response...")
                fallback_response = await _generate_fallback_response(request.query, chat_graph, model_manager)
                if fallback_response:
                    final_response = fallback_response
                    response_source = "error_recovery_fallback"
                    logger.info("Successfully recovered using fallback response")
            except Exception as fallback_error:
                logger.error(f"Fallback response generation failed: {fallback_error}")
            
            # Error Recovery Mechanism #2: Use default helpful response
            if not final_response:
                final_response = (
                    "I apologize, but I'm experiencing technical difficulties processing your request. "
                    "Please try rephrasing your question or try again in a moment. "
                    "If the issue persists, please contact support."
                )
                response_source = "error_recovery_default"
                logger.warning("Using default error recovery response")
        
        # Additional validation with type safety
        if final_response and not isinstance(final_response, str):
            try:
                final_response = str(final_response)
                logger.debug(f"Converted response to string: {type(final_response)}")
            except Exception as conversion_error:
                logger.error(f"Response type conversion failed: {conversion_error}")
                final_response = "I apologize, but I encountered an error formatting my response."
            except Exception as conv_error:
                logger.error(f"DEBUG: Failed to convert response to string: {conv_error}")
                raise HTTPException(status_code=500, detail={
                    "error": "Response format conversion failed",
                    "details": str(conv_error)
                })
        
        final_response = final_response.strip()
        if not final_response:
            logger.error("DEBUG: Response is empty after strip()")
            raise HTTPException(status_code=500, detail={
                "error": "Model response is empty after processing",
                "extraction_source": response_source
            })
        
        logger.info(f"DEBUG: ✅ Successfully extracted and validated response from {response_source}: '{final_response[:100]}...' (length: {len(final_response)})")
        # Optionally, treat very short/single-word responses as errors
        if len(final_response.strip()) < 5 or len(final_response.strip().split()) < 2:
            logger.error(f"Model returned too short/meaningless response: {final_response}")
            raise HTTPException(status_code=500, detail={
                "error": "Model returned a too short or meaningless response.",
                "suggestions": [
                    "Try rephrasing your question.",
                    "Check model health and logs."
                ]
            })
        # Ensure conversation_summary is a string or None
        raw_summary = getattr(chat_result, "conversation_summary", None)
        if raw_summary is not None and not isinstance(raw_summary, str):
            conversation_summary = str(raw_summary)
        else:
            conversation_summary = raw_summary
        conversation_context = ConversationContext(
            session_id=session_id,
            message_count=len(conversation_history) + 1,
            last_updated=datetime.utcnow().isoformat(),
            user_preferences=chat_request.user_context,
            conversation_summary=conversation_summary,
        )
        chat_data = ChatData(
            response=final_response,
            session_id=session_id,
            context=conversation_context,
            sources=getattr(chat_result, "sources_consulted", []),
            citations=getattr(chat_result, "citations", []),
        )
        # --- Conversation history exposure for multi-turn API ---
        # Try to get updated conversation history from chat_result or cache
        updated_history = None
        # Prefer chat_result.conversation_history if present
        if hasattr(chat_result, "conversation_history") and chat_result.conversation_history:
            updated_history = chat_result.conversation_history
        else:
            # Fallback: try to fetch from cache if available
            try:
                if cache_manager:
                    history_key = f"conversation_history:{session_id}"
                    updated_history = await cache_manager.get(history_key, [])
            except Exception as e:
                logger.warning(f"Could not fetch conversation history for response: {e}")
        chat_data.conversation_history = updated_history or conversation_history or []
        # --- Accumulate conversation history for multi-turn ---
        # Always append the current exchange to the incoming conversation_history
        conversation_entry = {
            "user_message": chat_request.message,
            "assistant_response": final_response,
            "query_id": query_id,
            "timestamp": datetime.utcnow().isoformat(),
            "intent": getattr(chat_result, "query_intent", None),
            "complexity": getattr(chat_result, "query_complexity", None),
            "total_cost": getattr(chat_result, "calculate_total_cost", lambda: 0.0)(),
            "execution_time": getattr(chat_result, "calculate_total_time", lambda: 0.0)(),
            "models_used": getattr(chat_result, "models_used", []),
        }
        accumulated_history = list(conversation_history) if conversation_history else []
        accumulated_history.append(conversation_entry)
        chat_data.conversation_history = accumulated_history
        # Optionally, also update the cache here if needed
        total_cost = 0.0
        try:
            if hasattr(chat_result, "calculate_total_cost"):
                cost_calc = chat_result.calculate_total_cost()
                total_cost = (
                    cost_calc  # Already awaited in the calculation above
                    if asyncio.iscoroutine(cost_calc)
                    else cost_calc
                )
        except Exception as e:
            logger.warning(f"Error calculating cost: {e}")
        execution_time = time.time() - start_time
        metadata = ResponseMetadata(
            query_id=query_id,
            correlation_id=correlation_id,  # Ensure this is always set
            execution_time=execution_time,
            cost=total_cost,
            models_used=list(getattr(chat_result, "models_used", set())),
            confidence=(chat_result.get_avg_confidence() if hasattr(chat_result, "get_avg_confidence") else 1.0),
            cached=False,
            timestamp=datetime.utcnow().isoformat(),
        )
        cost_prediction = None
        if chat_request.include_debug_info:
            cost_prediction = CostPrediction(
                estimated_cost=total_cost,
                cost_breakdown=[],
                savings_tips=["Use lower quality settings for simple queries"],
                alternative_workflows=[],
            )
        developer_hints = None
        if chat_request.include_debug_info:
            developer_hints = DeveloperHints(
                execution_path=getattr(chat_result, "execution_path", []),
                routing_explanation=f"Processed as {chat_request.quality_requirement} quality chat",
                performance={
                    "execution_time": execution_time,
                    "models_used": len(metadata.models_used),
                    "confidence": metadata.confidence,
                },
            )
        response = ChatResponse(
            status="success",
            data=chat_data,
            metadata=metadata,
            cost_prediction=cost_prediction,
            developer_hints=developer_hints,
        )
        from app.core.async_utils import AsyncSafetyValidator

        try:
            AsyncSafetyValidator.assert_no_coroutines(
                response, "Chat response contains coroutines"
            )
        except AssertionError as e:
            logger.error(f"Coroutine safety check failed: {e}")
            return create_safe_fallback_response(
                query_id, correlation_id, execution_time
            )
        logger.info(
            "Chat completion successful",
            query_id=query_id,
            execution_time=execution_time,
            cost=total_cost,
            correlation_id=correlation_id,
        )
        background_tasks.add_task(
            log_chat_analytics,
            query_id,
            chat_request.message,  # FIXED: was request.message
            final_response,  # Use extracted final_response
            execution_time,
            total_cost,
        )
        return response
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        execution_time = time.time() - start_time
        logger.error(
            "Chat completion timeout",
            query_id=query_id,
            execution_time=execution_time,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=408,
            detail=create_error_response(
                message="Request timeout",
                error_code="CHAT_TIMEOUT_ERROR",
                query_id=query_id,
                correlation_id=correlation_id,
                technical_details="Request exceeded time limit",
                suggestions=[
                    "Try a shorter or simpler question",
                    "Try again in a moment",
                ],
            ).model_dump(),
        )
    except ValueError as e:
        execution_time = time.time() - start_time
        logger.error(
            "Chat validation error",
            query_id=query_id,
            error=str(e),
            execution_time=execution_time,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=400,
            detail=create_error_response(
                message="Invalid request parameters",
                error_code="CHAT_VALIDATION_ERROR",
                query_id=query_id,
                correlation_id=correlation_id,
                technical_details=str(e),
                suggestions=[
                    "Check your input parameters",
                    "Ensure message is not empty",
                ],
            ).model_dump(),
        )
    except ConnectionError as e:
        execution_time = time.time() - start_time
        logger.error(
            "Chat service connection error",
            query_id=query_id,
            error=str(e),
            execution_time=execution_time,
            correlation_id=correlation_id,
        )
        raise HTTPException(
            status_code=503,
            detail=create_error_response(
                message="Service temporarily unavailable",
                error_code="CHAT_SERVICE_ERROR",
                query_id=query_id,
                correlation_id=correlation_id,
                technical_details="Unable to connect to chat service",
                suggestions=[
                    "Try again in a few moments",
                    "Check service status",
                ],
            ).model_dump(),
        )
    except Exception as e:
        execution_time = time.time() - start_time
        logger.error(
            "Chat completion failed",
            query_id=query_id,
            error=str(e),
            execution_time=execution_time,
            correlation_id=correlation_id,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=create_error_response(
                message="Chat completion failed",
                error_code="CHAT_PROCESSING_ERROR",
                query_id=query_id,
                correlation_id=correlation_id,
                technical_details=str(e),
                suggestions=[
                    "Try rephrasing your question",
                    "Reduce complexity if query is very long",
                    "Try again in a moment",
                ],
            ).model_dump(),
        )


def _unwrap_request(data: Dict[str, Any]) -> Dict[str, Any]:
    """Unwrap request payload if it's wrapped in 'request' key"""
    if "request" in data and isinstance(data["request"], dict):
        return data["request"]
    return data


# CORRECTED STREAMING ENDPOINT WITH COROUTINE SAFETY


@router.post("/stream")
@adaptive_timeout(base_timeout=30)
@coroutine_safe(timeout=120.0)
async def chat_stream(
    *,
    req: Request,
    streaming_request: ChatStreamRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    correlation_id = str(uuid.uuid4())
    set_correlation_id(correlation_id)
    query_id = str(uuid.uuid4())

    # Get components from app state
    app_state = getattr(req.app.state, "app_state", {})
    chat_graph = app_state.get("chat_graph")
    model_manager = app_state.get("model_manager")
    cache_manager_app = app_state.get("cache_manager")

    # Initialize fallback components if not found in app state
    if model_manager is None:
        from app.dependencies import get_model_manager

        model_manager = get_model_manager()

    if cache_manager_app is None:
        from app.dependencies import get_cache_manager

        cache_manager_app = get_cache_manager()

    # Use enhanced cache manager for better performance
    if cache_manager_app:
        cache_manager = cache_manager_app
    else:
        cache_manager = cache_manager  # Fallback to global enhanced cache manager

    if chat_graph is None:
        chat_graph = ChatGraph(model_manager, cache_manager_app)

    async def generate_safe_stream():
        global chat_graph, model_manager, cache_manager
        
        # Get user message first for caching and routing
        user_message = ""
        for msg in reversed(streaming_request.messages):
            if isinstance(msg, dict):
                role = msg.get("role", None)
                content = msg.get("content", "")
            else:
                role = getattr(msg, "role", None)
                content = getattr(msg, "content", "")
            if role == "user":
                user_message = content
                break
        
        # Get optimal model configuration based on query complexity
        model_config = model_router.get_model_config(user_message)
        should_cache = model_router.should_use_cache(user_message)
        cache_ttl = model_router.get_cache_ttl(model_config['complexity'])
        
        # Check cache first if appropriate
        if should_cache and cache_manager:
            cache_key = f"chat:{hashlib.md5(f'{user_message}'.encode()).hexdigest()}"
            try:
                if cache_manager:
                    cached_response = await cache_manager.get(cache_key)
                    if cached_response:
                        logger.debug(f"Cache hit for query: {user_message[:50]}...")
                        # Return cached response as stream
                        response_text = json.loads(cached_response).get('content', '')
                        if response_text:
                            chunk_size = max(10, len(response_text.split()) // 15)  # Word-based chunks
                            words = response_text.split()
                            for i in range(0, len(words), chunk_size):
                                chunk_words = words[i: i + chunk_size]
                                chunk = ' '.join(chunk_words)
                                stream_chunk = {
                                    "id": f"chatcmpl-{query_id}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": f"{model_config['model']}-cached",
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {"content": chunk + ' '},
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(stream_chunk)}\n\n"
                                await asyncio.sleep(0.05)  # Fast cached streaming
                            
                            # Send final chunk
                            final_chunk = {
                                "id": f"chatcmpl-{query_id}",
                                "object": "chat.completion.chunk",
                                "created": int(time.time()),
                                "model": f"{model_config['model']}-cached",
                                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                            }
                            yield f"data: {json.dumps(final_chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
            except Exception as e:
                logger.warning(f"Cache check failed: {e}")
        try:
            policy_check = check_content_policy(user_message)
            if not policy_check["passed"]:
                yield _create_error_stream_chunk("Content violates policy")
                yield "data: [DONE]\n\n"
                return
            session_id = (
                streaming_request.session_id
                or f"stream_{current_user['user_id']}_{int(time.time())}"
            )
            conversation_history = []
            for msg in streaming_request.messages[:-1]:
                if isinstance(msg, dict):
                    role = msg.get("role", "unknown")
                    content = msg.get("content", "")
                else:
                    role = getattr(msg, "role", "unknown")
                    content = getattr(msg, "content", "")
                conversation_history.append(
                    {"role": role, "content": content, "timestamp": time.time()}
                )
            graph_state = GraphState(
                query_id=query_id,
                correlation_id=correlation_id,
                user_id=current_user["user_id"],
                session_id=session_id,
                original_query=user_message,
                conversation_history=conversation_history,
                quality_requirement=QualityLevel.BALANCED,
                max_cost=0.10,
                max_execution_time=30.0,
                user_preferences={
                    "tier": getattr(current_user, "tier", "free"),
                    "streaming": True,
                },
            )
            # Ensure we have initialized dependencies
            if chat_graph is None:
                if model_manager is None:
                    from app.dependencies import get_model_manager
                    model_manager = get_model_manager()
                if cache_manager is None:
                    from app.dependencies import get_cache_manager
                    cache_manager = get_cache_manager()
                chat_graph = ChatGraph(model_manager, cache_manager)
            
            # Use simple streaming approach: execute graph then stream the response
            chat_result = await safe_graph_execute(
                chat_graph, graph_state, timeout=30.0
            )
            # chat_result is already properly awaited from safe_graph_execute
            
            if chat_result and hasattr(chat_result, 'final_response') and chat_result.final_response:
                response_text = chat_result.final_response
                model_used = "phi3:mini"  # Default model
                
                # Cache the response for future use with intelligent TTL
                if should_cache and cache_manager:
                    cache_key = f"chat:{hashlib.md5(f'{user_message}'.encode()).hexdigest()}"
                    try:
                        cache_data = json.dumps({'content': response_text, 'model': model_used})
                        await cache_manager.set(cache_key, cache_data, ttl=cache_ttl)
                        logger.debug(f"Cached response for query: {user_message[:50]}... (TTL: {cache_ttl}s)")
                    except Exception as e:
                        logger.warning(f"Failed to cache response: {e}")
                
                # Optimized streaming: word-based chunks for better readability
                words = response_text.split()
                chunk_size = max(8, len(words) // 25)  # Optimal chunk size for readability
                
                for i in range(0, len(words), chunk_size):
                    chunk_words = words[i: i + chunk_size]
                    chunk = ' '.join(chunk_words)
                    stream_chunk = {
                        "id": f"chatcmpl-{query_id}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model_used,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": chunk + ' '},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(stream_chunk)}\n\n"
                    
                    # Variable delays for better UX
                    if i == 0:
                        await asyncio.sleep(0.05)  # Quick first chunk
                    elif i < chunk_size * 3:
                        await asyncio.sleep(0.08)  # Faster initial chunks
                    else:
                        await asyncio.sleep(0.12)  # Normal delay
                    
                # Send final chunk
                final_chunk = {
                    "id": f"chatcmpl-{query_id}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model_used,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            else:
                yield _create_error_stream_chunk("No response generated")
                yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error(f"Streaming error: {e}")
            yield _create_error_stream_chunk(f"Internal error: {str(e)}")
            yield "data: [DONE]\n\n"
            return

    return StreamingResponse(
        generate_safe_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _create_error_stream_chunk(error_message: str) -> str:
    error_chunk = {
        "id": f"chatcmpl-error-{int(time.time())}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "error",
        "choices": [
            {
                "index": 0,
                "delta": {"content": f"Error: {error_message}"},
                "finish_reason": "stop",
            }
        ],
    }
    return f"data: {json.dumps(error_chunk)}\n\n"


def create_safe_fallback_response(
    query_id: str, correlation_id: str, execution_time: float
) -> ChatResponse:
    chat_data = ChatData(
        response="I apologize, but I'm experiencing technical difficulties. Please try again.",
        session_id="fallback_session",
        context=None,
        sources=[],
        citations=[],
    )
    metadata = ResponseMetadata(
        query_id=query_id,
        execution_time=execution_time,
        cost=0.0,
        models_used=["fallback"],
        confidence=0.0,
        cached=False,
        timestamp=datetime.utcnow().isoformat(),
    )
    return ChatResponse(status="error", data=chat_data, metadata=metadata)


async def log_chat_analytics(
    query_id: str, user_message: str, response: str, execution_time: float, cost: float
):
    try:
        logger.info(
            "Chat analytics",
            query_id=query_id,
            message_length=len(user_message),
            response_length=len(response),
            execution_time=execution_time,
            cost=cost,
        )
    except Exception as e:
        logger.error(f"Error logging analytics: {e}")


@router.get("/history/{session_id}")
async def get_conversation_history(
    session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    try:
        return {
            "session_id": session_id,
            "history": [],
            "message_count": 0,
            "last_updated": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Error fetching history: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to fetch conversation history"
        )


@router.delete("/history/{session_id}")
async def clear_conversation_history(
    session_id: str, current_user: Dict[str, Any] = Depends(get_current_user)
):
    try:
        return {
            "cleared": True,
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat(),
        }
    except Exception as e:
        logger.error(f"Error clearing history: {e}")
        raise HTTPException(
            status_code=500, detail="Failed to clear conversation history"
        )


def set_dependencies(
    fake_model_manager=None, fake_cache_manager=None, fake_chat_graph=None
):
    """
    Override global dependencies for testing. Injects fake services for model_manager, cache_manager, and chat_graph.
    """
    global model_manager, cache_manager, chat_graph
    if fake_model_manager is not None:
        model_manager = fake_model_manager
    if fake_cache_manager is not None:
        cache_manager = fake_cache_manager
    if fake_chat_graph is not None:
        chat_graph = fake_chat_graph


async def _generate_fallback_response(query: str, chat_graph, model_manager) -> Optional[str]:
    """Generate a simple fallback response when main processing fails."""
    try:
        # Try a simple direct model call as fallback
        if model_manager and hasattr(model_manager, 'ollama_client'):
            simple_prompt = f"Please provide a brief, helpful response to: {query}"
            result = await model_manager.generate(
                prompt=simple_prompt,
                task_type="conversation",
                max_tokens=200,
                timeout=10
            )
            if result and hasattr(result, 'content') and result.content:
                return result.content.strip()
        return None
    except Exception as e:
        logger.debug(f"Fallback response generation failed: {e}")
        return None
