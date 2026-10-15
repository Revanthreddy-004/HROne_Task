Employee Attendance & Analytics API

FastAPI and MongoDB backend for employee attendance, punch-in/punch-out, regularization, and attendance analytics.

Run locally

1. Install dependencies: `pip install -r requirements.txt`
2. Configure `MONGO_URI` and `MONGO_DB` as environment variables.
3. Start the application: `uvicorn app.main:app --port 8000`
4. Open `http://localhost:8000/docs` to explore the API.

The application should create its required indexes at startup.

Notes

See `REVIEW.md` for code-review findings and `DECISIONS.md` for implementation decisions. Large-dataset performance and any remaining limitations should be reported only after verification.