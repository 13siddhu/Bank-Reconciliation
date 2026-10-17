from django.http import HttpResponse


class CorsMiddleware:
    """
    Lightweight CORS middleware for local API development.
    Handles preflight OPTIONS and adds CORS response headers.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "OPTIONS":
            response = HttpResponse()
        else:
            response = self.get_response(request)

        allowed_origins = {
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        }
        origin = request.headers.get("Origin") or request.META.get("HTTP_ORIGIN", "")
        if origin in allowed_origins:
            response["Access-Control-Allow-Origin"] = origin
            response["Vary"] = "Origin"
        else:
            response["Access-Control-Allow-Origin"] = "http://localhost:5173"

        response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response["Access-Control-Allow-Headers"] = (
            "Content-Type, X-Organization, Authorization, X-Requested-With"
        )
        return response
