import asyncio

from faultmaven.data_processing.classifier import DataClassifier

TEST_CASES = [
    ("2024-01-01 12:00:00 ERROR [app] Database connection failed", "?"),
    ("[ERROR] Failed to connect to database", "?"),
    ("INFO: Application started successfully", "?"),
    ("DEBUG: Processing request ID 12345", "?"),
    ("Exception: java.lang.NullPointerException", "?"),
    ("Error: Division by zero", "?"),
    ("Stack trace: at com.example.App.main", "?"),
    ("cpu_usage{host='server1'} 85.2", "?"),
    ("memory_usage_percent 67.8", "?"),
    ("http_requests_total{method='GET'} 1234", "?"),
    ("database.host=localhost", "?"),
    ("api.timeout=30", "?"),
    ("logging.level=DEBUG", "?"),
    ("This is a troubleshooting guide.", "?"),
    ("Some random text that doesn't match patterns", "?"),
]


async def main():
    classifier = DataClassifier()
    for text, _ in TEST_CASES:
        result = await classifier.classify(text)
        print(f"Input: {text!r}\n  Output: {result}\n")


if __name__ == "__main__":
    asyncio.run(main())
