# FaultMaven User Guide

Welcome to FaultMaven! This guide will help you get the open-source version of the FaultMaven server up and running on your local machine.

## 1. System Requirements

* **Python:** Version 3.10 or higher
* **Pip:** Python's package installer
* **Docker & Docker Compose:** For running backing services easily

## 2. Installation

Follow these steps to set up the FaultMaven application.

1.  **Clone the Repository**
    Open your terminal and clone the official FaultMaven repository:
    ```bash
    git clone [https://github.com/your-org/faultmaven.git](https://github.com/your-org/faultmaven.git)
    cd faultmaven
    ```

2.  **Set Up a Virtual Environment**
    It's highly recommended to use a Python virtual environment to manage dependencies.
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows, use `venv\Scripts\activate`
    ```

3.  **Install Dependencies**
    Install all the required Python packages using the `requirements.txt` file.
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment Variables**
    You will need to configure API keys for the LLM provider you intend to use. Copy the example environment file and edit it with your keys.
    ```bash
    cp config/settings.py.example config/settings.py
    # Now, open config/settings.py and add your API keys
    ```

## 3. Running the Server

The easiest way to run the server and its dependencies is with Docker Compose.

* **Start the Application:**
    This command will build the necessary Docker images and start the FaultMaven FastAPI server, which will be accessible at `http://localhost:8000`.
    ```bash
    docker-compose up --build
    ```

## 4. Quickstart: How to Use FaultMaven

You can interact with the FaultMaven server using any API client (like `curl`, Postman, or a custom script). The server has two main endpoints: `/data` and `/query`.

### Step 1: Submit Data

First, let's submit a log file to start a new session. This `curl` command sends a log file to the `/data` endpoint. The server will respond with an initial analysis and a new `session-id`.

```bash
curl -X POST -F "file=@/path/to/your/logfile.log" http://localhost:8000/data
```
Response: You'll get back a JSON object with insights and a session_id. Make sure to save the session-id for the next step.

### Step 2: Ask a Question

Now, use the session-id you received to ask a question about the data you just uploaded. This curl command sends a query to the /query endpoint, passing the session-id in the headers.

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Session-ID: <your-session-id-from-step-1>" \
  -d '{"query": "What are the most common errors in the data I just uploaded?"}' \
  http://localhost:8000/query
```
Response: FaultMaven will provide a detailed answer based on its analysis of the log file in the context of your session.