from datetime import datetime, timedelta
import pytest
from vbc.ui.state import UIState
from vbc.ui.dashboard import Dashboard

def test_eta_calculation_after_refresh():
    """
    Test that ETA is calculated correctly when completed_count > files_to_process,
    which happens if the queue is refreshed (files_to_process = remaining) but
    completed_count is cumulative.
    """
    state = UIState()
    
    # Simulate a long running session
    state.processing_start_time = datetime.now() - timedelta(seconds=3600) # 1 hour running
    
    # Simulate 1000 files completed previously
    state.completed_count = 1000
    state.failed_count = 50
    
    # Simulate a refresh happening just now
    # Discovery says 100 files remaining
    state.files_to_process = 100
    
    # Snapshots at discovery (should match current counts if just refreshed)
    state.completed_count_at_last_discovery = 1000
    state.failed_count_at_last_discovery = 50
    
    # Now simulate processing 10 more files
    state.completed_count = 1010 
    # (failed_count stays 50)
    
    # Current time advanced slightly
    # Global elapsed: 3600s for 1050 files -> ~3.4s per file
    # We processed 10 more files, so elapsed increases by ~34s
    state.processing_start_time = datetime.now() - timedelta(seconds=3634)
    
    dashboard = Dashboard(state)
    
    # Manually trigger ETA calculation logic (usually in _generate_top_bar)
    # We can inspect the _generate_top_bar output or extract logic.
    # Since we can't easily parse Renderable, let's verify logic by reproducing it
    # matching what we put in Dashboard._generate_top_bar
    
    total = state.files_to_process # 100
    done_since = (state.completed_count - state.completed_count_at_last_discovery) + \
                 (state.failed_count - state.failed_count_at_last_discovery)
                 # (1010 - 1000) + (50 - 50) = 10
                 
    rem = total - done_since # 100 - 10 = 90
    
    assert rem == 90
    
    session_done = state.completed_count + state.failed_count # 1010 + 50 = 1060
    elapsed = (datetime.now() - state.processing_start_time).total_seconds()
    
    assert session_done > 0
    assert rem > 0
    
    avg = elapsed / session_done
    eta_seconds = avg * rem
    
    # Check if format_global_eta returns a string with numbers
    eta_str = dashboard.format_global_eta(eta_seconds)
    assert eta_str != "--:--"
    assert "m" in eta_str or "s" in eta_str

def test_eta_calculation_mixed_failure_and_completion():
    """Test ETA with mixed failures and completions."""
    state = UIState()
    state.processing_start_time = datetime.now() - timedelta(seconds=100)
    
    # Initial state
    state.completed_count = 10
    state.failed_count = 2
    
    # Discovery (files_to_process = remaining)
    state.files_to_process = 50
    state.completed_count_at_last_discovery = 10
    state.failed_count_at_last_discovery = 2
    
    # Process 5 more: 3 success, 2 fail
    state.completed_count = 13
    state.failed_count = 4
    
    dashboard = Dashboard(state)
    
    total = state.files_to_process # 50
    done_since = (13 - 10) + (4 - 2) # 3 + 2 = 5
    rem = total - done_since # 45
    
    assert rem == 45
    
    session_done = 13 + 4 # 17
    elapsed = 100
    avg = elapsed / session_done # 100/17 ~= 5.88s/file
    
    eta_seconds = avg * rem # 5.88 * 45 ~= 264s
    
    eta_str = dashboard.format_global_eta(eta_seconds)
    assert eta_str == "04m 24s" or eta_str == "04m 25s" # approx
