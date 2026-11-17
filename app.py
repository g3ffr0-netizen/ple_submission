attempt_count_container = [0]

    def count_attempt(attempt_num, error_msg, wait_seconds):
        attempt_count_container[0] = attempt_num

    with st.status("Executing harness inference...", expanded=True) as status:
        try:
            result = engine.run_inference_with_status(
                prompt=task_prompt, 
                status_callback=count_attempt
            )
            
            attempt_count = attempt_count_container[0]
            if attempt_count == 0:
                attempt_count = 1
