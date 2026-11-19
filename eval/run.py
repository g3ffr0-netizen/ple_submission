import os
import json
import subprocess
from pathlib import Path

def run_eval():
    # 1. Paths
    repo_root = Path(__file__).parent.parent
    artifacts_dir = repo_root / "artifacts"
    dev_tasks_dir = repo_root / "dev_tasks" # Assuming you have this folder
    
    artifacts_dir.mkdir(exist_ok=True)

    # 2. Start the serve.py harness
    print("Starting harness server...")
    server_process = subprocess.Popen(
        ["python", "serve.py"],
        cwd=repo_root
    )

    try:
        # 3. Loop through dev tasks (replace with your actual task loading)
        # This is a placeholder. You need to load the 29 dev tasks.
        dev_tasks = [f for f in os.listdir(dev_tasks_dir) if f.endswith('.json')]
        
        for task_file in dev_tasks:
            task_path = dev_tasks_dir / task_file
            print(f"Processing {task_file}...")
            
            with open(task_path, 'r') as f:
                task_data = json.load(f)
            
            # 4. Send task to harness (via WebSocket or HTTP)
            # This is a placeholder. You need to implement the communication.
            # response = send_to_harness(task_data)
            
            # 5. Save artifact
            artifact_path = artifacts_dir / f"{task_file}.json"
            with open(artifact_path, 'w') as f:
                json.dump({"status": "placeholder"}, f)
                
            print(f"Saved {artifact_path}")

    finally:
        # 6. Kill the server
        server_process.terminate()
        server_process.wait()
        print("Harness server stopped.")

if __name__ == "__main__":
    run_eval()
