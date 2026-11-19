import asyncio
import json
import websockets
from harness import HarnessExecutionEngine

# Initialize the engine
engine = HarnessExecutionEngine()

async def handle_task(websocket, path):
    async for message in websocket:
        try:
            data = json.loads(message)
            prompt = data.get("prompt", "")
            
            # Run the harness
            result = engine.run_harness(prompt)
            
            # Send the result back
            await websocket.send(json.dumps(result))
        except Exception as e:
            await websocket.send(json.dumps({"status": "FAILED", "reason": str(e)}))

async def main():
    async with websockets.serve(handle_task, "0.0.0.0", 8080):
        print("Harness server running on port 8080")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())
