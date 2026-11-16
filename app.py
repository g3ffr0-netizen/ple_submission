try:
    from harness import HarnessExecutionEngine
    HAS_HARNESS = True
except ImportError:
    try:
        from ple_submission_harness import HarnessExecutionEngine
        HAS_HARNESS = True
    except ImportError:
        HAS_HARNESS = False
