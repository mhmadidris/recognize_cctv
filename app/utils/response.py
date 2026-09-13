from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from app.enums.response_status import ResponseStatus
from typing import Any

def create_response(
    status: ResponseStatus, 
    message: str, 
    result: Any = None, 
    errors: Any = None,
    status_code: int = 200
) -> JSONResponse:
    """Helper function to create a JSONResponse with a standardized format."""
    content = {
        "message": message,
        "result": result,
        "errors": errors
    }
    return JSONResponse(status_code=status_code, content=jsonable_encoder(content))

def success_response(message: str, result: Any = None, status_code: int = 200) -> JSONResponse:
    return create_response(ResponseStatus.SUCCESS, message, result=result, status_code=status_code)

def error_response(message: str, errors: Any = None, status_code: int = 400) -> JSONResponse:
    return create_response(ResponseStatus.ERROR, message, errors=errors, status_code=status_code)
