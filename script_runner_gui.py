# Script Runner - Manage & Automate Scripts
# Built by SmaRTy Saini
# LinkedIn: https://www.linkedin.com/in/smartysaini/
# GitHub:   https://github.com/SmaRTy-Saini/
# Store:    https://smartysaini.gumroad.com/

import sys
import subprocess
import threading
import os
import json
import csv
import platform
import signal
from datetime import datetime
import psutil  # For process management (pause/resume)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore # For persistent scheduling
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QFileDialog, QVBoxLayout,
    QHBoxLayout, QWidget, QTableWidget, QTableWidgetItem, QTextEdit, QMessageBox,
    QInputDialog, QLineEdit, QLabel, QComboBox, QTabWidget, QSplitter, QProgressBar,
    QHeaderView, QDialog, QDateTimeEdit, QFormLayout, QSpinBox
)
from PyQt5.QtGui import QIcon, QTextCharFormat, QColor, QFont, QPixmap
from PyQt5.QtCore import Qt, pyqtSignal, QObject, QThread, QMutex, QTimer, QDateTime

# Get a writable config directory inside AppData
APPDATA_DIR = os.path.join(os.getenv("APPDATA"), "ScriptRunner")
os.makedirs(APPDATA_DIR, exist_ok=True)  # Ensure it exists

# Define all important paths inside the writable directory
CONFIG_FILE = os.path.join(APPDATA_DIR, "scripts_config.json")
LOG_FILE = os.path.join(APPDATA_DIR, "script_logs.txt")
SCHEDULE_DB = os.path.join(APPDATA_DIR, "schedule.sqlite")
INTERPRETER_SETTINGS_FILE = os.path.join(APPDATA_DIR, "interpreter_settings.json")


WINDOW = None

def run_scheduled_script(script):
    global WINDOW
    if WINDOW is None:
        print("Window not initialized")
        return
    WINDOW.run_script_scheduled(script)

class OutputEmitter(QObject):
    output_signal = pyqtSignal(str, str)
    status_signal = pyqtSignal(str, str)  # path, status
    progress_signal = pyqtSignal(int)

class ScriptExecutionThread(QThread):
    def __init__(self, script, parameters, emitter, main_window):
        super().__init__()
        self.script = script
        self.parameters = parameters
        self.emitter = emitter
        self.main_window = main_window
        self.process = None
        self.is_paused = False
        self.is_stopped = False
        self.mutex = QMutex() # Mutex for this thread's internal state (is_paused, is_stopped)
        
    def run(self):
        script_path = self.script['path']
        script_type = self.script['type']

        self.emitter.status_signal.emit(script_path, "Running")
        self.emitter.output_signal.emit(f"\n[START] {self.script.get('tag', '')} {os.path.basename(script_path)}\n", "blue")
        
        interpreter_path = self.main_window.get_interpreter_path(script_type)
        
        cmd = []
        
        try:
            if script_type == "Python":
                cmd = [interpreter_path, script_path]
            elif script_type == "PowerShell":
                if platform.system() == "Windows":
                    cmd = [interpreter_path, "-ExecutionPolicy", "Bypass", "-File", script_path]
                else: # PowerShell Core for Linux/Mac
                    cmd = [interpreter_path, "-File", script_path]
            elif script_type == "Batch":
                if platform.system() == "Windows":
                    cmd = [script_path]
                else:
                    self.emitter.output_signal.emit("Batch files are native to Windows and not directly supported on this platform.\n", "red")
                    self.emitter.status_signal.emit(script_path, "Error")
                    self.write_log(script_path, -1, "Batch files not supported on non-Windows platforms")
                    return
            elif script_type == "Shell":
                if platform.system() != "Windows":
                    cmd = [interpreter_path, script_path]
                else:
                    self.emitter.output_signal.emit("Shell scripts (.sh) are typically for Unix-like systems. On Windows, consider using Git Bash or WSL.\n", "orange")
                    cmd = [interpreter_path, script_path] # Fallback to attempting 'bash' directly
            elif script_type == "Go":
                # For Go, we usually run the source file directly or a compiled executable
                if script_path.endswith(".go"):
                    cmd = [interpreter_path, "run", script_path]
                else: # Assume it's a compiled executable
                    cmd = [script_path]
            elif script_type == "Rust":
                # For Rust, similar to Go, run source via cargo or a compiled executable
                if os.path.basename(script_path) == "Cargo.toml" or os.path.isdir(script_path):
                    # If it's a Cargo.toml or a directory, assume it's a Rust project
                    cmd = [interpreter_path, "run", "--manifest-path", os.path.join(script_path, "Cargo.toml") if os.path.isdir(script_path) else script_path]
                    # Change CWD to the script's directory for cargo to find project
                    self.process_cwd = os.path.dirname(script_path) if not os.path.isdir(script_path) else script_path
                else: # Assume it's a compiled executable
                    cmd = [script_path]
            elif script_type == "Java":
                if script_path.lower().endswith(".jar"):
                    cmd = [interpreter_path, "-jar", script_path]
                elif script_path.lower().endswith(".java"):
                    # For .java, first compile then run. This might be tricky in one go.
                    # Best practice is to compile outside, or provide a .class path.
                    # For simplicity, this assumes a compiled .class in a standard location
                    # A more advanced approach would compile on the fly.
                    class_name = os.path.splitext(os.path.basename(script_path))[0]
                    class_dir = os.path.dirname(script_path)
                    cmd = [interpreter_path, "-cp", class_dir, class_name]
                else:
                    self.emitter.output_signal.emit("Unsupported Java file type. Please provide a .jar or .java file (and ensure it's compiled or handle compilation manually).\n", "red")
                    self.emitter.status_signal.emit(script_path, "Error")
                    self.write_log(script_path, -1, "Unsupported Java file type.")
                    return
            elif script_type == "Ruby":
                cmd = [interpreter_path, script_path]
            elif script_type == "C#":
                # For C#, typically execute a compiled .exe on Windows
                if platform.system() == "Windows":
                    if script_path.lower().endswith(".exe"):
                        cmd = [script_path]
                    elif os.path.basename(script_path).lower() == "csproj" or script_path.lower().endswith(".csproj"):
                        # If it's a .NET project file, use dotnet run
                        cmd = [interpreter_path, "run", "--project", script_path]
                        self.process_cwd = os.path.dirname(script_path) # CWD to project directory
                    else:
                        self.emitter.output_signal.emit("Unsupported C# file type. Please provide a compiled .exe or a .csproj file.\n", "red")
                        self.emitter.status_signal.emit(script_path, "Error")
                        self.write_log(script_path, -1, "Unsupported C# file type.")
                        return
                else: # For Linux/Mac, try with 'mono' or 'dotnet'
                    # User needs to ensure mono or dotnet is installed and configured
                    if script_path.lower().endswith(".exe"): # Mono executable
                         cmd = [interpreter_path, script_path]
                    elif os.path.basename(script_path).lower() == "csproj" or script_path.lower().endswith(".csproj"):
                        cmd = [interpreter_path, "run", "--project", script_path]
                        self.process_cwd = os.path.dirname(script_path)
                    else:
                        self.emitter.output_signal.emit("Unsupported C# file type for non-Windows. Provide a compiled .exe (for Mono) or a .csproj file (for dotnet).\n", "red")
                        self.emitter.status_signal.emit(script_path, "Error")
                        self.write_log(script_path, -1, "Unsupported C# file type.")
                        return
            else:
                self.emitter.output_signal.emit(f"Unsupported script type: {script_type}.\n", "red")
                self.emitter.status_signal.emit(script_path, "Error")
                self.write_log(script_path, -1, f"Unsupported script type: {script_type}")
                return

            if self.parameters:
                cmd.extend(self.parameters.split())

            # Define CWD for subprocess, especially important for Go/Rust/C# projects
            cwd_for_process = getattr(self, 'process_cwd', None) # Get if set, else None

            # Create process
            # Add shell=True for batch files on Windows if cmd is just the script path
            shell_needed = script_type == "Batch" and platform.system() == "Windows" and len(cmd) == 1
            
            self.process = subprocess.Popen(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.STDOUT, # Merge stdout and stderr for simpler reading
                text=True,
                bufsize=1,  # Line-buffered
                universal_newlines=True,
                shell=shell_needed, # Only for batch files and cases where shell is truly needed
                cwd=cwd_for_process # Set working directory if defined
            )
            
            # Verify process was created successfully
            if self.process is None:
                self.emitter.output_signal.emit("[ERROR] Failed to create process\n", "red")
                self.emitter.status_signal.emit(script_path, "Error")
                self.write_log(script_path, -1, "Failed to create process")
                return
            
            # Handle output reading differently based on platform
            try:
                if platform.system() == "Windows":
                    self._read_output_windows()
                else:
                    self._read_output_unix()
            except Exception as e:
                self.emitter.output_signal.emit(f"[ERROR] Output reading failed: {str(e)}. Falling back to simpler read.\n", "red")
                self._read_output_fallback()
            
            if not self.is_stopped and self.process:
                try:
                    self.process.wait()
                    exit_code = self.process.returncode
                    
                    if exit_code == 0:
                        self.emitter.output_signal.emit(f"[SUCCESS] Finished with code {exit_code}\n", "green")
                    else:
                        self.emitter.output_signal.emit(f"[ERROR] Finished with code {exit_code}\n", "red")
                    
                    self.write_log(script_path, exit_code)
                except Exception as e:
                    self.emitter.output_signal.emit(f"[ERROR] Process wait failed: {str(e)}\n", "red")
                    self.write_log(script_path, -1, str(e))
            else:
                self.emitter.output_signal.emit("[STOPPED] Script execution stopped by user\n", "orange")
                self.write_log(script_path, -1, "Stopped by user")
                
        except FileNotFoundError:
            error_msg = f"Command not found: '{cmd[0]}'. Please ensure the required interpreter is installed and its path is configured in Settings (⚙️)."
            self.emitter.output_signal.emit(f"[ERROR] {error_msg}\n", "red")
            self.write_log(script_path, -1, error_msg)
        except PermissionError:
            error_msg = f"Permission denied to execute '{script_path}'. Check file permissions or run Script Runner as administrator."
            self.emitter.output_signal.emit(f"[ERROR] {error_msg}\n", "red")
            self.write_log(script_path, -1, error_msg)
        except Exception as e:
            self.emitter.output_signal.emit(f"[ERROR] {str(e)}\n", "red")
            self.write_log(script_path, -1, str(e))
        finally:
            self.emitter.status_signal.emit(script_path, "Idle")
            # Ensure process is truly dead after completion/error
            if self.process and self.process.poll() is None:
                try:
                    self.process.kill()
                except:
                    pass
    
    def _read_output_windows(self):
        """Windows-specific output reading with proper pause/resume support"""
        if not self.process or not self.process.stdout:
            self.emitter.output_signal.emit("[ERROR] No process or stdout available for Windows output reading\n", "red")
            return
            
        import queue
        import threading
        
        output_queue = queue.Queue()
        
        def reader_thread_func():
            try:
                for line in iter(self.process.stdout.readline, ''):
                    if line:
                        output_queue.put(line.rstrip())
                    else:
                        break
                output_queue.put(None)  # Signal end of stream
            except Exception as e:
                output_queue.put(f"[ERROR] Reader thread error: {e}")
        
        reader = threading.Thread(target=reader_thread_func, daemon=True)
        reader.start()
        
        psutil_imported = False
        try:
            import psutil
            psutil_imported = True
        except ImportError:
            self.emitter.output_signal.emit("[WARNING] 'psutil' not found. Process pause/resume will be flag-based only.\n", "orange")
        
        while True:
            self.mutex.lock() # Lock for is_stopped, is_paused
            stopped = self.is_stopped
            paused = self.is_paused
            self.mutex.unlock()

            if stopped:
                try:
                    if self.process and self.process.poll() is None:
                        self.process.terminate()
                        self.process.wait(timeout=1) # Give a moment to terminate
                        if self.process.poll() is None:
                            self.process.kill()
                except Exception as e:
                    self.emitter.output_signal.emit(f"[STOP ERROR] {str(e)}\n", "red")
                break
            
            # Handle pause state (suspend/resume actual process)
            if paused:
                if psutil_imported and self.process and self.process.poll() is None:
                    try:
                        proc = psutil.Process(self.process.pid)
                        proc.suspend()
                    except psutil.NoSuchProcess:
                        break # Process already ended
                    except Exception as e:
                        self.emitter.output_signal.emit(f"[PAUSE WARNING] psutil suspend failed: {str(e)}\n", "orange")
                self.msleep(100) # Sleep while paused
                continue # Skip output reading while paused
            else: # Not paused, try to resume if it was suspended
                if psutil_imported and self.process and self.process.poll() is None:
                    try:
                        proc = psutil.Process(self.process.pid)
                        proc.resume()
                    except psutil.NoSuchProcess:
                        pass # Process already ended
                    except Exception as e:
                        self.emitter.output_signal.emit(f"[RESUME WARNING] psutil resume failed: {str(e)}\n", "orange")

            try:
                line = output_queue.get(timeout=0.05) # Small timeout to allow checks
                if line is None:  # End signal from reader thread
                    break
                
                color = self.get_output_color(line)
                self.emitter.output_signal.emit(line, color)
                
            except queue.Empty:
                if not reader.is_alive() and (self.process is None or self.process.poll() is None):
                    # Reader thread finished and process completed, no more output expected
                    break
                continue
            except Exception as e:
                self.emitter.output_signal.emit(f"[ERROR] Output processing failed: {str(e)}\n", "red")
                break
        
        # After loop, ensure reader thread is joined
        if reader.is_alive():
            reader.join(timeout=1) # Give it a moment to finish

    def _read_output_unix(self):
        """Unix-specific output reading with select for non-blocking I/O"""
        if not self.process or not self.process.stdout:
            self.emitter.output_signal.emit("[ERROR] No process or stdout available for Unix output reading\n", "red")
            return

        try:
            import select
            import fcntl
            import os

            fd = self.process.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

            buffer = ""
            while True:
                self.mutex.lock()
                stopped = self.is_stopped
                paused = self.is_paused
                self.mutex.unlock()

                if stopped:
                    try:
                        if self.process and self.process.poll() is None:
                            self.process.terminate()
                            self.process.wait(timeout=1)
                            if self.process.poll() is None:
                                self.process.kill()
                    except Exception as e:
                        self.emitter.output_signal.emit(f"[STOP ERROR] {str(e)}\n", "red")
                    break

                if paused:
                    if self.process and self.process.poll() is None:
                        try:
                            os.kill(self.process.pid, signal.SIGSTOP)
                        except Exception as e:
                            self.emitter.output_signal.emit(f"[PAUSE WARNING] Failed to SIGSTOP process: {str(e)}\n", "orange")
                    self.msleep(100)
                    continue
                else:
                    if self.process and self.process.poll() is None:
                        try:
                            os.kill(self.process.pid, signal.SIGCONT)
                        except Exception as e:
                            self.emitter.output_signal.emit(f"[RESUME WARNING] Failed to SIGCONT process: {str(e)}\n", "orange")

                try:
                    ready, _, _ = select.select([self.process.stdout], [], [], 0.05)
                    if ready:
                        try:
                            chunk = self.process.stdout.read(4096)
                            if chunk:
                                buffer += chunk
                                while '\n' in buffer:
                                    line, buffer = buffer.split('\n', 1)
                                    if line.strip():
                                        color = self.get_output_color(line.strip())
                                        self.emitter.output_signal.emit(line.strip(), color)
                            else:
                                break
                        except BlockingIOError:
                            pass
                        except Exception as e:
                            self.emitter.output_signal.emit(f"[ERROR] Reading stdout chunk failed: {str(e)}\n", "red")
                            break

                    if self.process.poll() is None:
                        if buffer.strip():
                            color = self.get_output_color(buffer.strip())
                            self.emitter.output_signal.emit(buffer.strip(), color)
                        break

                except select.error as e:
                    if e.errno != 4:
                        self.emitter.output_signal.emit(f"[ERROR] Select error: {str(e)}\n", "red")
                    break
                except Exception as e:
                    self.emitter.output_signal.emit(f"[ERROR] Unix output reading failed: {str(e)}. Falling back.\n", "red")
                    self._read_output_fallback()

        except Exception as e: # This handles exceptions during the setup of Unix reading (e.g., import errors)
            self.emitter.output_signal.emit(f"[ERROR] Initialization of Unix reading failed: {str(e)}\n", "red")
            self._read_output_fallback() # Fallback also for init errors


    def _read_output_fallback(self):
        """Fallback method for basic output reading (blocking)"""
        try:
            if not self.process or not self.process.stdout:
                self.emitter.output_signal.emit("[ERROR] No process or stdout available for fallback output reading\n", "red")
                return
                
            for line in iter(self.process.stdout.readline, ''):
                self.mutex.lock()
                stopped = self.is_stopped
                paused = self.is_paused
                self.mutex.unlock()

                if stopped:
                    try:
                        self.process.terminate()
                        self.process.wait(timeout=1)
                        if self.process.poll() is None:
                            self.process.kill()
                    except:
                        pass
                    break
                    
                while paused:
                    self.msleep(100)
                    self.mutex.lock()
                    paused = self.is_paused
                    stopped = self.is_stopped # Re-check stopped state while paused
                    self.mutex.unlock()
                    if stopped: break # If stopped while paused, break outer loop

                if stopped: break # Break outer loop if stopped after pause check

                line = line.strip()
                if line:
                    color = self.get_output_color(line)
                    self.emitter.output_signal.emit(line, color)
        except Exception as e:
            self.emitter.output_signal.emit(f"[ERROR] Error reading output (fallback): {e}\n", "red")
    
    def get_output_color(self, line):
        """Determine output color based on keywords"""
        line_lower = line.lower()
        
        if any(keyword in line_lower for keyword in ['error', 'exception', 'failed', 'fail', 'critical', 'denied']):
            return "red"
        elif any(keyword in line_lower for keyword in ['warning', 'warn', 'caution', 'pause', 'resume', 'stopping', 'skipped']):
            return "orange"
        elif any(keyword in line_lower for keyword in ['success', 'completed', 'done', 'finished', 'ok']):
            return "green"
        elif any(keyword in line_lower for keyword in ['info', 'debug', 'log', 'start', 'end', 'scheduled run']):
            return "cyan"
        else:
            return None
    
    def pause(self):
        self.mutex.lock()
        if not self.is_stopped:
            self.is_paused = True
            if self.process and self.process.poll() is None:
                try:
                    import psutil
                    proc = psutil.Process(self.process.pid)
                    if platform.system() == "Windows":
                        proc.suspend()
                        self.emitter.output_signal.emit(f"[PAUSED] Process {self.process.pid} suspended\n", "orange")
                    else: # Unix
                        if proc.status() != psutil.STATUS_STOPPED:
                            os.kill(self.process.pid, signal.SIGSTOP)
                            self.emitter.output_signal.emit(f"[PAUSED] Process {self.process.pid} stopped\n", "orange")
                except ImportError:
                    self.emitter.output_signal.emit("[PAUSED] Process paused (flag-based, psutil not found)\n", "orange")
                except psutil.NoSuchProcess:
                    self.emitter.output_signal.emit("[PAUSED] Process already finished, cannot suspend.\n", "orange")
                except Exception as e:
                    self.emitter.output_signal.emit(f"[PAUSE WARNING] Failed to suspend/stop process: {str(e)}\n", "orange")
            else:
                self.emitter.output_signal.emit("[PAUSED] No active process to pause\n", "orange")
        self.mutex.unlock()
    
    def resume(self):
        self.mutex.lock()
        if not self.is_stopped:
            self.is_paused = False
            if self.process and self.process.poll() is None:
                try:
                    import psutil
                    proc = psutil.Process(self.process.pid)
                    if platform.system() == "Windows":
                        proc.resume()
                        self.emitter.output_signal.emit(f"[RESUMED] Process {self.process.pid} resumed\n", "green")
                    else: # Unix
                        if proc.status() == psutil.STATUS_STOPPED:
                            os.kill(self.process.pid, signal.SIGCONT)
                            self.emitter.output_signal.emit(f"[RESUMED] Process {self.process.pid} continued\n", "green")
                except ImportError:
                    self.emitter.output_signal.emit("[RESUMED] Process resumed (flag-based, psutil not found)\n", "green")
                except psutil.NoSuchProcess:
                    self.emitter.output_signal.emit("[RESUMED] Process already finished, cannot resume.\n", "green")
                except Exception as e:
                    self.emitter.output_signal.emit(f"[RESUME WARNING] Failed to resume/continue process: {str(e)}\n", "orange")
            else:
                self.emitter.output_signal.emit("[RESUMED] No active process to resume\n", "green")
        self.mutex.unlock()
    
    def stop(self):
        self.mutex.lock()
        if not self.is_stopped: # Prevent multiple stop calls
            self.is_stopped = True
            if self.process:
                self.emitter.output_signal.emit(f"[STOPPING] Terminating process {self.process.pid}...\n", "orange")
                try:
                    # First try graceful termination
                    self.process.terminate()
                    # Wait a bit for graceful shutdown
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        # Force kill if terminate doesn't work
                        try:
                            self.process.kill()
                            self.emitter.output_signal.emit(f"[FORCE STOP] Force killed process {self.process.pid}\n", "red")
                        except Exception as kill_e:
                            self.emitter.output_signal.emit(f"[FORCE STOP ERROR] {str(kill_e)}\n", "red")
                            
                except Exception as e:
                    self.emitter.output_signal.emit(f"[STOP ERROR] {str(e)}\n", "red")
            else:
                self.emitter.output_signal.emit("[STOPPED] No active process to stop\n", "orange")
        self.mutex.unlock()
    
    def write_log(self, script_path, code, error=None):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_entry = {
            "timestamp": timestamp,
            "script": script_path,
            "exit_code": code,
            "error": error if error else "None"
        }
        try:
            # Read existing logs, append new, write back to handle potential partial writes
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 0: # Check if file exists and is not empty
                with open(LOG_FILE, 'r', encoding='utf-8') as f:
                    try:
                        existing_logs = [json.loads(line) for line in f if line.strip()]
                    except json.JSONDecodeError:
                        self.emitter.output_signal.emit(f"[LOG ERROR] Corrupted log file '{LOG_FILE}'. Starting fresh.\n", "red")
                        existing_logs = []
            else:
                existing_logs = []
            
            existing_logs.append(log_entry)

            with open(LOG_FILE, 'w', encoding='utf-8') as log_file:
                for entry in existing_logs:
                    log_file.write(json.dumps(entry) + '\n')
            
        except Exception as e:
            self.emitter.output_signal.emit(f"Failed to write log: {str(e)}\n", "red")

class ManageSchedulesDialog(QDialog):
    def __init__(self, parent=None, scheduler=None):
        super().__init__(parent)
        self.setWindowTitle("⏰ Manage Scheduled Scripts")
        self.setGeometry(200, 200, 800, 500)
        self.scheduler = scheduler
        self.init_ui()

    def init_ui(self):
        main_layout = QVBoxLayout(self)

        self.schedule_table = QTableWidget()
        self.schedule_table.setColumnCount(4)
        self.schedule_table.setHorizontalHeaderLabels(["ID", "Script Path", "Trigger", "Next Run"])
        self.schedule_table.setSelectionBehavior(self.schedule_table.SelectRows)
        self.schedule_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.schedule_table.horizontalHeader().setStretchLastSection(True) # Make last column stretch

        main_layout.addWidget(self.schedule_table)

        button_layout = QHBoxLayout()
        refresh_btn = QPushButton("🔄 Refresh")
        refresh_btn.clicked.connect(self.load_schedules)
        remove_btn = QPushButton("🗑 Remove Selected")
        remove_btn.clicked.connect(self.remove_selected_schedule)
        
        button_layout.addStretch()
        button_layout.addWidget(refresh_btn)
        button_layout.addWidget(remove_btn)
        button_layout.addStretch()

        main_layout.addLayout(button_layout)
        self.load_schedules()

    def load_schedules(self):
        self.schedule_table.setRowCount(0)
        if not self.scheduler or not self.scheduler.running:
            QMessageBox.warning(self, "Scheduler Error", "Scheduler is not running.")
            return

        jobs = self.scheduler.get_jobs()
        for job in jobs:
            row_idx = self.schedule_table.rowCount()
            self.schedule_table.insertRow(row_idx)

            script_path = "N/A"
            if job.args and len(job.args) > 0 and isinstance(job.args[0], dict) and 'path' in job.args[0]:
                script_path = job.args[0]['path']
            
            trigger_info = str(job.trigger)
            next_run = job.next_run_time.strftime('%Y-%m-%d %H:%M:%S') if job.next_run_time else "No next run"

            self.schedule_table.setItem(row_idx, 0, QTableWidgetItem(job.id))
            self.schedule_table.setItem(row_idx, 1, QTableWidgetItem(script_path))
            self.schedule_table.setItem(row_idx, 2, QTableWidgetItem(trigger_info))
            self.schedule_table.setItem(row_idx, 3, QTableWidgetItem(next_run))
        
        self.schedule_table.resizeColumnsToContents()


    def remove_selected_schedule(self):
        selected_rows = sorted(set(index.row() for index in self.schedule_table.selectedIndexes()), reverse=True)
        if not selected_rows:
            QMessageBox.warning(self, "No Selection", "Please select a scheduled script to remove.")
            return

        confirm = QMessageBox.question(
            self, "Confirm Removal",
            f"Are you sure you want to remove {len(selected_rows)} scheduled job(s)?",
            QMessageBox.Yes | QMessageBox.No
        )

        if confirm == QMessageBox.Yes:
            removed_count = 0
            for row in selected_rows:
                job_id = self.schedule_table.item(row, 0).text()
                try:
                    self.scheduler.remove_job(job_id)
                    removed_count += 1
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to remove job '{job_id}': {str(e)}")
            if removed_count > 0:
                QMessageBox.information(self, "Removed", f"Removed {removed_count} scheduled job(s).")
            self.load_schedules() # Refresh the list

class InterpreterSettingsDialog(QDialog):
    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ Interpreter Settings")
        self.settings = settings if settings is not None else {}
        self.init_ui()

    def init_ui(self):
        layout = QFormLayout(self)

        self.interpreter_fields = {}
        # Define interpreters and their default values. Provide common executables.
        interpreters = {
            'Python': sys.executable,
            'PowerShell': 'powershell.exe' if platform.system() == "Windows" else 'pwsh',
            'Batch': 'cmd.exe' if platform.system() == "Windows" else '', # Batch files are cmd directly, no interpreter needed usually
            'Shell': 'bash' if platform.system() != "Windows" else 'bash.exe', # For Git Bash, WSL bash
            'Go': 'go', # go run, go build
            'Rust': 'cargo', # cargo run
            'Java': 'java', # java -jar, java -cp
            'Ruby': 'ruby',
            'C#': 'dotnet' if platform.system() != "Windows" else 'dotnet.exe' # for dotnet run (cross-platform), or 'mono.exe'/'mono'
        }

        for name, default_path in interpreters.items():
            line_edit = QLineEdit(self.settings.get(name, default_path))
            browse_btn = QPushButton("Browse...")
            browse_btn.clicked.connect(lambda checked, le=line_edit, n=name: self.browse_interpreter(le, f"{n} Interpreter"))
            
            h_layout = QHBoxLayout()
            h_layout.addWidget(line_edit)
            h_layout.addWidget(browse_btn)
            layout.addRow(f"{name} Interpreter:", h_layout)
            self.interpreter_fields[name] = line_edit


        buttons_layout = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject) # Corrected: Was `dialog.reject`, should be `self.reject`
        buttons_layout.addStretch()
        buttons_layout.addWidget(save_btn)
        buttons_layout.addWidget(cancel_btn)
        layout.addRow(buttons_layout)

    def browse_interpreter(self, line_edit, title):
        # On Windows, filter for .exe, .cmd, .bat; otherwise, filter all files.
        filter_str = "Executables (*.exe);;All Files (*)"
        if platform.system() != "Windows":
             filter_str = "All Files (*)" # On Unix, executables don't typically have extensions

        executable_path, _ = QFileDialog.getOpenFileName(self, title, "", filter_str)
        if executable_path:
            line_edit.setText(executable_path)

    def get_settings(self):
        updated_settings = {}
        for name, line_edit in self.interpreter_fields.items():
            updated_settings[name] = line_edit.text().strip()
        return updated_settings

class ScriptRunner(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(QIcon("Logo.ico"))
        self.setWindowTitle("🛠️ Script Runner - Manage & Automate Scripts")
        self.setGeometry(100, 100, 1400, 900)

        self.script_list = self.load_scripts()
        self.interpreter_settings = self.load_interpreter_settings()
        
        # Configure APScheduler with SQLAlchemyJobStore for persistence
        jobstores = {
            'default': SQLAlchemyJobStore(url=f'sqlite:///{SCHEDULE_DB}')
        }
        self.scheduler = BackgroundScheduler(jobstores=jobstores)
        try:
            self.scheduler.start()
        except Exception as e:
            QMessageBox.critical(self, "Scheduler Error", f"Failed to start scheduler. Scheduled jobs might not run or save correctly: {str(e)}")
            # Fallback for scheduling if persistence fails, but warn user
            self.scheduler = BackgroundScheduler() 
            self.scheduler.start()
            
        # Track running threads
        self.running_threads = {}
        self.execution_mutex = QMutex() # Protects access to self.running_threads

        self.emitter = OutputEmitter()
        self.emitter.output_signal.connect(self.print_output)
        self.emitter.status_signal.connect(self.set_status)

        self.init_ui()
        
        # --- Pre-installation and welcome information on app load ---
        self.show_pre_installation_info()
        # --- End pre-installation info ---

        self.append_disclaimer()
        
        # Auto-save timer
        self.auto_save_timer = QTimer()
        self.auto_save_timer.timeout.connect(self.save_scripts)
        self.auto_save_timer.start(30000)  # Auto-save every 30 seconds

    def show_pre_installation_info(self):
        info_message = (
            "Welcome to Script Runner!\n\n"
            "To ensure this tool works correctly with all script types, "
            "please make sure you have the following installed on your system:\n\n"
            "•  Python (required for the app itself)\n"
            "•  PowerShell (for .ps1 scripts)\n"
            "•  Git Bash / WSL (for .sh scripts on Windows, 'bash' command)\n"
            "•  Go (for .go programs)\n"
            "•  Rust (for .rs programs, requires 'cargo')\n"
            "•  Java JDK/JRE (for .java, .jar files)\n"
            "•  Ruby (for .rb scripts)\n"
            "•  .NET SDK or Mono (for .csproj, .exe C# programs)\n\n"
            "You can configure specific interpreter paths in '⚙️ Settings' if they are not in your system's PATH.\n\n"
            "For full pause/resume functionality on Windows, it's recommended to install 'psutil':\n"
            "pip install psutil\n\n"
            "Click 'OK' to continue to the application."
        )
        QMessageBox.information(self, "Essential Pre-requisites", info_message)


    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # Create tabs
        self.tabs = QTabWidget()
        self.init_main_tab()
        self.init_how_to_tab()
        
        main_layout.addWidget(self.tabs)
        
        # Footer at the bottom
        self.init_footer()
        main_layout.addWidget(self.footer)

    def init_main_tab(self):
        self.main_tab = QWidget()
        layout = QVBoxLayout(self.main_tab)

        # Control buttons
        self.init_control_buttons(layout)
        
        # Create splitter for table and output
        splitter = QSplitter(Qt.Vertical)
        
        # Table widget
        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(["Type", "Path", "Description", "Parameters", "Tag/Icon", "Status"])
        self.table.setSelectionBehavior(self.table.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive) # Allow manual resize
        self.table.horizontalHeader().setStretchLastSection(True) # Last column stretches
        self.table.setEditTriggers(QTableWidget.NoEditTriggers) # Disable direct editing in table initially
        
        self.load_table()
        
        # Output console - Create self.output first
        output_widget = QWidget()
        output_layout = QVBoxLayout(output_widget)
        
        # Create the output widget first
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas", 10))
        self.output.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #ffffff;
                border: 1px solid #555;
            }
        """)
        
        # Console controls - Now we can reference self.output
        console_controls = QHBoxLayout()
        
        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.clicked.connect(self.pause_execution)
        self.pause_btn.setEnabled(False)
        
        self.resume_btn = QPushButton("▶ Resume")
        self.resume_btn.clicked.connect(self.resume_execution)
        self.resume_btn.setEnabled(False)
        
        self.stop_btn = QPushButton("⏹ Stop All") # Changed to Stop All for clarity
        self.stop_btn.clicked.connect(self.stop_execution)
        self.stop_btn.setEnabled(False)
        
        clear_btn = QPushButton("🧹 Clear Console")
        clear_btn.clicked.connect(self.output.clear)
        
        console_controls.addWidget(QLabel("🔧 Script Output Console:"))
        console_controls.addStretch()
        console_controls.addWidget(self.pause_btn)
        console_controls.addWidget(self.resume_btn)
        console_controls.addWidget(self.stop_btn)
        console_controls.addWidget(clear_btn)
        
        output_layout.addLayout(console_controls)
        output_layout.addWidget(self.output)
        
        # Add to splitter
        splitter.addWidget(self.table)
        splitter.addWidget(output_widget)
        splitter.setSizes([300, 200]) # Initial sizes
        
        layout.addWidget(splitter)
        
        self.tabs.addTab(self.main_tab, "Script Manager")

    def init_control_buttons(self, layout):
        # Primary actions
        primary_layout = QHBoxLayout()
        
        add_btn = QPushButton("➕ Add Script")
        add_btn.clicked.connect(self.add_script)
        add_btn.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; font-weight: bold; }")
        
        run_btn = QPushButton("▶ Run Selected")
        run_btn.clicked.connect(self.run_selected)
        run_btn.setStyleSheet("QPushButton { background-color: #2196F3; color: white; font-weight: bold; }")
        
        edit_btn = QPushButton("✏️ Edit Selected")
        edit_btn.clicked.connect(self.edit_selected_script)
        edit_btn.setStyleSheet("QPushButton { background-color: #FFC107; color: black; font-weight: bold; }") # Yellowish
        
        delete_btn = QPushButton("🗑 Delete Selected")
        delete_btn.clicked.connect(self.delete_selected)
        delete_btn.setStyleSheet("QPushButton { background-color: #f44336; color: white; font-weight: bold; }")
        
        for btn in [add_btn, run_btn, edit_btn, delete_btn]: # Added edit_btn
            primary_layout.addWidget(btn)
        
        primary_layout.addStretch()
        
        # Secondary actions
        secondary_layout = QHBoxLayout()
        
        schedule_btn = QPushButton("⏰ Schedule Script")
        schedule_btn.clicked.connect(self.schedule_script)

        manage_schedules_btn = QPushButton("🗓️ Manage Schedules")
        manage_schedules_btn.clicked.connect(self.manage_schedules)
        
        log_btn = QPushButton("📜 View Logs")
        log_btn.clicked.connect(self.view_logs)
        
        export_btn = QPushButton("📤 Export Scripts")
        export_btn.clicked.connect(self.export_scripts)
        
        import_btn = QPushButton("📥 Import Scripts")
        import_btn.clicked.connect(self.import_scripts)
        
        export_logs_btn = QPushButton("📁 Export Logs (CSV)")
        export_logs_btn.clicked.connect(self.export_logs_csv)

        settings_btn = QPushButton("⚙️ Settings")
        settings_btn.clicked.connect(self.open_settings)
        
        for btn in [schedule_btn, manage_schedules_btn, log_btn, export_btn, import_btn, export_logs_btn, settings_btn]: # Added manage_schedules_btn, settings_btn
            secondary_layout.addWidget(btn)
        
        layout.addLayout(primary_layout)
        layout.addLayout(secondary_layout)

    def init_how_to_tab(self):
        tab = QWidget()
        layout = QVBoxLayout()
        instructions = QTextEdit()
        instructions.setReadOnly(True)
        instructions.setFont(QFont("Segoe UI", 10))
        instructions.setStyleSheet("QTextEdit { background-color: #f9f9f9; color: #333; border: none; }")
        instructions.setHtml("""
        <h2>🚀 Getting Started with Script Runner</h2>

        <h3>🔧 Installation & Setup:</h3>
        <ol>
            <li>
                <b>Download & Extract:</b> Get the latest release from GitHub or Gumroad and extract the ZIP file.
            </li>
            <li>
                <b>Install Dependencies:</b> Open a terminal/command prompt in the extracted folder and run:
                <pre><code>pip install -r requirements.txt</code></pre>
                <p>This ensures you have everything needed. Key libraries include:</p>
                <ul>
                    <li><code>PyQt5</code>: For the graphical interface.</li>
                    <li><code>APScheduler</code>: For powerful script scheduling.</li>
                    <li><code>SQLAlchemy</code>: For persistent scheduling across app restarts.</li>
                    <li><code>psutil</code>: <span style="color: #17A2B8; font-weight: bold;">(Recommended for Windows)</span> Enhances process control (pause/resume). Install with <code>pip install psutil</code>.</li>
                </ul>
            </li>
            <li>
                <b>Run the Application:</b> Execute the main script:
                <pre><code>python script_runner_gui.py</code></pre>
            </li>
        </ol>

        <h3>🖥️ Basic Usage:</h3>
        <ol>
            <li>
                <b>Add Your Scripts:</b> Click the <span style="background-color: #4CAF50; color: white; padding: 2px 5px; border-radius: 3px;">➕ Add Script</span> button. Select your script file (supports <code>.py</code>, <code>.ps1</code>, <code>.bat</code>, <code>.cmd</code>, <code>.sh</code>, <code>.go</code>, <code>.rs</code>, <code>.java</code>, <code>.jar</code>, <code>.csproj</code>, <code>.exe</code>, <code>.rb</code>). You can add a description, default parameters, and a unique tag (like an emoji!).
            </li>
            <li>
                <b>Run Instantly:</b> Select one or more scripts from the table and click <span style="background-color: #2196F3; color: white; padding: 2px 5px; border-radius: 3px;">▶ Run Selected</span>. A dialog will appear for any additional parameters.
            </li>
            <li>
                <b>Control Execution:</b> While a script is running, use the <span style="background-color: #ff9800; color: white; padding: 2px 5px; border-radius: 3px;">⏸ Pause</span>, <span style="background-color: #4CAF50; color: white; padding: 2px 5px; border-radius: 3px;">▶ Resume</span>, and <span style="background-color: #f44336; color: white; padding: 2px 5px; border-radius: 3px;">⏹ Stop All</span> buttons.
            </li>
            <li>
                <b>Automate with Scheduling:</b> Select a script and click <span style="background-color: #607D8B; color: white; padding: 2px 5px; border-radius: 3px;">⏰ Schedule Script</span> to set it to run at a specific interval or date/time.
            </li>
            <li>
                <b>Monitor Output:</b> The 'Script Output Console' provides real-time feedback with color-coded messages for easy understanding.
            </li>
            <li>
                <b>Manage Scripts:</b>
                <ul>
                    <li><span style="background-color: #FFC107; color: black; padding: 2px 5px; border-radius: 3px;">✏️ Edit Selected:</span> Modify the description, parameters, or tag of an existing script.</li>
                    <li><span style="background-color: #f44336; color: white; padding: 2px 5px; border-radius: 3px;">🗑 Delete Selected:</span> Remove scripts from your list.</li>
                    <li><span style="background-color: #607D8B; color: white; padding: 2px 5px; border-radius: 3px;">🗓️ Manage Schedules:</span> View, pause, resume, or remove scheduled jobs.</li>
                    <li><span style="background-color: #607D8B; color: white; padding: 2px 5px; border-radius: 3px;">📤 Export Scripts / 📥 Import Scripts:</span> Backup or transfer your script configurations.</li>
                    <li><span style="background-color: #607D8B; color: white; padding: 2px 5px; border-radius: 3px;">📁 Export Logs (CSV):</span> Save your execution logs in a spreadsheet-friendly format.</li>
                </ul>
            </li>
        </ol>

        <h3>✨ Advanced Features:</h3>
        <ul>
            <li><b>Flexible Parameters:</b> Define default parameters for scripts or override them dynamically before execution.</li>
            <li><b>Comprehensive Logging:</b> Every script execution is recorded with timestamp, exit code, and error details in <code>script_logs.txt</code>.</li>
            <li><b>Cross-Platform Compatibility:</b> Works seamlessly on Windows, Linux, and macOS for various script types.</li>
            <li><b>Intelligent Process Control:</b> Utilizes native OS capabilities (with <code>psutil</code> installed) to truly pause and resume running scripts.</li>
            <li><b>Color-Coded Console:</b> Quickly identify success, warnings, and errors in the output.</li>
            <li><b>Persistent Schedules:</b> Your scheduled jobs are saved and will resume even after closing and reopening the application.</li>
            <li><b>Custom Interpreter Paths:</b> Configure specific paths for Python, PowerShell, Shell (Bash), Go, Rust, Java, Ruby, and C# (.NET/Mono) interpreters in the <span style="background-color: #607D8B; color: white; padding: 2px 5px; border-radius: 3px;">⚙️ Settings</span>.</li>
        </ul>

        <h3>🎨 Understanding Console Colors:</h3>
        <ul style="color: #333;">
            <li><span style="color: red; font-weight: bold;">🔴 Red:</span> Indicates critical issues, errors, or exceptions.</li>
            <li><span style="color: orange; font-weight: bold;">🟠 Orange:</span> Signifies warnings, cautions, or state changes (like pausing).</li>
            <li><span style="color: green; font-weight: bold;">🟢 Green:</span> Represents successful operations and completions.</li>
            <li><span style="color: blue; font-weight: bold;">🔵 Blue:</span> Provides general information, debug messages, or standard output.</li>
        </ul>

        <h3>⚠️ Important Safety & Troubleshooting Tips:</h3>
        <ul>
            <li><b>Trust Your Scripts:</b> Always verify the source and purpose of any script before running it.</li>
            <li><b>Permissions:</b> Ensure scripts have necessary permissions. Run Script Runner as administrator if system-level access is required.</li>
            <li><b>Dependencies:</b> Confirm that the required interpreters (e.g., Python, PowerShell, Go, Java) are installed and accessible via your system's PATH, or configure their explicit paths in <span style="background-color: #607D8B; color: white; padding: 2px 5px; border-radius: 3px;">⚙️ Settings</span>.</li>
            <li><b>Compilations:</b> For compiled languages like Go, Rust, Java, or C#, ensure your scripts are compiled into executables or class/jar files that the respective interpreters can run. Some language types (Rust .csproj, C# .csproj) allow running directly from project files using `cargo run` or `dotnet run` if the SDK is installed.</li>
            <li><b>Backup:</b> It's always wise to back up important data before running scripts that modify system settings or files.</li>
            <li><b>Need Help?</b> If a script isn't found, check its path and interpreter settings. For process control issues on Windows, ensure <code>psutil</code> is installed via <code>pip install psutil</code>.</li>
            <li><b>Log File Size:</b> The <code>script_logs.txt</code> file can grow large. You might want to periodically clear it or manage its size manually.</li>
        </ul>
        """)
        layout.addWidget(instructions)
        tab.setLayout(layout)
        self.tabs.addTab(tab, "📘 How to Use")

    def init_footer(self):
        self.footer = QWidget()
        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(10, 5, 10, 5) # Added padding

        # Left aligned info
        left_label = QLabel(
            '<p style="margin:0; padding:0;">Made with ❤️ by <b>SmaRTy Saini</b></p>'
        )
        left_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        left_label.setOpenExternalLinks(True)

        # Center aligned links
        links_html = """
        <p style="margin:0; padding:0;">
            <a href="https://github.com/SmaRTy-Saini" style="color:#2196F3; text-decoration:none;">GitHub</a> •
            <a href="https://www.linkedin.com/in/smartysaini/" style="color:#0077B5; text-decoration:none;">LinkedIn</a> •
            <a href="https://smartysaini.gumroad.com/" style="color:#E6007A; text-decoration:none;">Gumroad</a> •
            <a href="https://x.com/SmaRTy__Saini" style="color:#1DA1F2; text-decoration:none;">X (Twitter)</a>
        </p>
        """
        links_label = QLabel(links_html)
        links_label.setAlignment(Qt.AlignCenter | Qt.AlignVCenter)
        links_label.setOpenExternalLinks(True)

        # Right aligned application info
        right_label = QLabel(f"Script Runner v{QApplication.applicationVersion()}")
        right_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        footer_layout.addWidget(left_label)
        footer_layout.addStretch()
        footer_layout.addWidget(links_label)
        footer_layout.addStretch()
        footer_layout.addWidget(right_label)

        self.footer.setStyleSheet("""
            QWidget {
                background-color: #f0f0f0;
                border-top: 1px solid #d0d0d0;
            }
            QLabel {
                font-size: 10pt;
                color: #555;
            }
            a {
                text-decoration: none;
                color: #2196F3;
            }
            a:hover {
                text-decoration: underline;
            }
        """)

    def print_output(self, text, color=None):
        # Use a QTextCursor for more robust text appending and formatting
        cursor = self.output.textCursor()
        cursor.movePosition(cursor.End)
        
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        else:
            fmt.setForeground(QColor("white")) # Default color for console
            
        cursor.insertText(text + "\n", fmt) # Add newline
        self.output.setTextCursor(cursor) # Ensure cursor is at end
        
        # Auto-scroll to bottom
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def load_scripts(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                QMessageBox.warning(self, "Config Error", f"Failed to load scripts config: {str(e)}\n"
                                                           "A new config file will be created.")
                return []
        return []

    def save_scripts(self):
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.script_list, f, indent=4, ensure_ascii=False)
        except Exception as e:
            QMessageBox.warning(self, "Save Error", f"Failed to save scripts config: {str(e)}")

    def load_interpreter_settings(self):
        # Default settings for various interpreters
        settings = {
            'Python': sys.executable,
            'PowerShell': 'powershell.exe' if platform.system() == "Windows" else 'pwsh',
            'Batch': 'cmd.exe' if platform.system() == "Windows" else '', # Batch files are cmd directly, no interpreter needed usually
            'Shell': 'bash' if platform.system() != "Windows" else 'bash.exe',
            'Go': 'go',
            'Rust': 'cargo',
            'Java': 'java',
            'Ruby': 'ruby',
            'C#': 'dotnet' if platform.system() != "Windows" else 'dotnet.exe' # Default to dotnet for cross-platform, users can change to mono.exe
        }
        if os.path.exists(INTERPRETER_SETTINGS_FILE):
            try:
                with open(INTERPRETER_SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    loaded_settings = json.load(f)
                    settings.update(loaded_settings) # Update defaults with loaded settings
            except Exception as e:
                QMessageBox.warning(self, "Settings Error", f"Failed to load interpreter settings: {str(e)}")
        return settings

    def save_interpreter_settings(self):
        try:
            with open(INTERPRETER_SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.interpreter_settings, f, indent=4, ensure_ascii=False)
        except Exception as e:
            QMessageBox.warning(self, "Settings Error", f"Failed to save interpreter settings: {str(e)}")

    def get_interpreter_path(self, interpreter_type):
        # Fallback to default if not found in settings for some reason
        default_interpreters = {
            'Python': sys.executable,
            'PowerShell': 'powershell.exe' if platform.system() == "Windows" else 'pwsh',
            'Batch': 'cmd.exe' if platform.system() == "Windows" else '',
            'Shell': 'bash' if platform.system() != "Windows" else 'bash.exe',
            'Go': 'go',
            'Rust': 'cargo',
            'Java': 'java',
            'Ruby': 'ruby',
            'C#': 'dotnet' if platform.system() != "Windows" else 'dotnet.exe'
        }
        return self.interpreter_settings.get(interpreter_type, default_interpreters.get(interpreter_type, interpreter_type.lower()))


    def open_settings(self):
        dialog = InterpreterSettingsDialog(self, self.interpreter_settings)
        if dialog.exec_() == QDialog.Accepted:
            new_settings = dialog.get_settings()
            self.interpreter_settings.update(new_settings)
            self.save_interpreter_settings()
            QMessageBox.information(self, "Settings Saved", "Interpreter settings have been updated.")

    def load_table(self):
        self.table.setRowCount(0)
        for script in self.script_list:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(script['type']))
            self.table.setItem(row, 1, QTableWidgetItem(script['path']))
            self.table.setItem(row, 2, QTableWidgetItem(script.get('description', '')))
            self.table.setItem(row, 3, QTableWidgetItem(script.get('parameters', '')))
            self.table.setItem(row, 4, QTableWidgetItem(script.get('tag', '')))
            self.table.setItem(row, 5, QTableWidgetItem("Idle"))
        
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True) # Ensure it remains stretching

    def set_status(self, path, status):
        for row in range(self.table.rowCount()):
            if self.table.item(row, 1).text() == path:
                self.table.setItem(row, 5, QTableWidgetItem(status))
                break
        
        # Update button states based on overall running status
        self.update_control_button_states()

    def add_script(self):
        # Expanded file types for more languages
        file_types = (
            "All Supported Scripts (*.py *.ps1 *.bat *.cmd *.sh *.go *.rs *.java *.jar *.csproj *.exe *.rb);;"
            "Python Scripts (*.py);;"
            "PowerShell Scripts (*.ps1);;"
            "Batch Files (*.bat *.cmd);;"
            "Shell Scripts (*.sh);;"
            "Go Programs (*.go);;"
            "Rust Programs (*.rs);;"
            "Java Programs (*.java *.jar);;"
            "Ruby Scripts (*.rb);;" # Ruby extension is typically .rb
            "C#/.NET Programs (*.csproj *.exe);;"
            "All Files (*)"
        )
        filepath, _ = QFileDialog.getOpenFileName(self, "Select Script", "", file_types)
        
        if filepath:
            ext = os.path.splitext(filepath)[1].lower()
            filename_lower = os.path.basename(filepath).lower() # For .csproj / Cargo.toml

            script_type = "Unknown"
            if ext == ".py": script_type = "Python"
            elif ext == ".ps1": script_type = "PowerShell"
            elif ext in [".bat", ".cmd"]: script_type = "Batch"
            elif ext == ".sh": script_type = "Shell"
            elif ext == ".go": script_type = "Go"
            elif ext == ".rs": script_type = "Rust"
            elif ext in [".java", ".jar"]: script_type = "Java"
            elif ext == ".rb": script_type = "Ruby"
            elif ext == ".exe" and platform.system() == "Windows": script_type = "C#" # Compiled C# exe on Windows
            elif ext == ".csproj" or filename_lower == "cargo.toml": # .NET project or Rust project
                if ext == ".csproj": script_type = "C#"
                elif filename_lower == "cargo.toml": script_type = "Rust"

            if script_type == "Unknown":
                QMessageBox.warning(self, "Invalid Script", "Unsupported file type or language. Supported: .py, .ps1, .bat, .cmd, .sh, .go, .rs, .java, .jar, .rb, .csproj, .exe (for Windows C#).")
                return

            # Check if script already exists to prevent duplicates based on path
            if any(s['path'] == filepath for s in self.script_list):
                QMessageBox.warning(self, "Duplicate Script", "This script is already in your list.")
                return

            desc, ok_desc = QInputDialog.getText(self, 'Script Description', 'Enter a friendly description (e.g., "Daily Report Generator"):')
            if not ok_desc:
                return
                
            params, ok_params = QInputDialog.getText(self, 'Default Parameters', 'Enter default command-line parameters (optional):')
            tag, ok_tag = QInputDialog.getText(self, 'Script Tag/Icon', 'Enter a short tag or emoji (e.g., "📊", "🔥", "⚙️"):')
            
            new_script = {
                "type": script_type,
                "path": filepath,
                "description": desc,
                "parameters": params if ok_params else "",
                "tag": tag if ok_tag else ""
            }
            
            self.script_list.append(new_script)
            self.save_scripts()
            self.load_table()
            QMessageBox.information(self, "Success", "Script added successfully!")

    def edit_selected_script(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "No Selection", "Please select a script to edit.")
            return
        if len(selected_rows) > 1:
            QMessageBox.warning(self, "Multiple Selection", "Please select only one script to edit.")
            return

        row = selected_rows[0].row()
        script_to_edit = self.script_list[row]

        desc, ok_desc = QInputDialog.getText(self, 'Edit Script Description', 'Enter a friendly description:',
                                             QLineEdit.Normal, script_to_edit.get('description', ''))
        if not ok_desc:
            return

        params, ok_params = QInputDialog.getText(self, 'Edit Default Parameters', 'Enter default command-line parameters (optional):',
                                                 QLineEdit.Normal, script_to_edit.get('parameters', ''))
        if not ok_params:
            return

        tag, ok_tag = QInputDialog.getText(self, 'Edit Script Tag/Icon', 'Enter a short tag or emoji (e.g., "📊", "🔥", "⚙️"):',
                                           QLineEdit.Normal, script_to_edit.get('tag', ''))
        if not ok_tag:
            return

        script_to_edit['description'] = desc
        script_to_edit['parameters'] = params
        script_to_edit['tag'] = tag

        self.save_scripts()
        self.load_table() # Reload table to reflect changes
        QMessageBox.information(self, "Success", "Script updated successfully!")

    def delete_selected(self):
        rows = sorted(set(index.row() for index in self.table.selectedIndexes()), reverse=True)
        if not rows:
            QMessageBox.warning(self, "No Selection", "Please select a script to delete.")
            return
            
        confirm = QMessageBox.question(
            self, "Confirm Delete", 
            f"Are you sure you want to delete {len(rows)} script(s)? This will also remove any associated schedules.",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if confirm == QMessageBox.Yes:
            scripts_to_remove = []
            for row in rows:
                script_to_remove = self.script_list[row]
                scripts_to_remove.append(script_to_remove)
            
            for script in scripts_to_remove:
                script_path = script['path']
                
                # Stop any running execution of this script
                self.execution_mutex.lock()
                if script_path in self.running_threads:
                    self.running_threads[script_path].stop()
                    # It will be removed from running_threads by cleanup_thread signal
                self.execution_mutex.unlock()

                # Remove any associated scheduled jobs
                for job in self.scheduler.get_jobs():
                    if job.args and len(job.args) > 0 and isinstance(job.args[0], dict) and job.args[0].get('path') == script_path:
                        try:
                            self.scheduler.remove_job(job.id)
                            self.emitter.output_signal.emit(f"[INFO] Removed scheduled job '{job.id}' for '{os.path.basename(script_path)}'.\n", "blue")
                        except Exception as e:
                            self.emitter.output_signal.emit(f"[ERROR] Failed to remove scheduled job '{job.id}': {str(e)}\n", "red")
                
                # Remove from the main list after handling associated items
                if script in self.script_list: # Check again in case it was already removed by another thread's delete
                    self.script_list.remove(script)
            
            self.save_scripts()
            self.load_table()
            QMessageBox.information(self, "Success", f"Deleted {len(rows)} script(s) and their schedules.")
            self.update_control_button_states() # Update button states after potential stops


    def run_selected(self):
        rows = sorted(set(index.row() for index in self.table.selectedIndexes()))
        if not rows:
            QMessageBox.warning(self, "No Selection", "Please select a script to run.")
            return

        for row in rows:
            script = self.script_list[row]
            script_path = script['path']
            
            self.execution_mutex.lock()
            if script_path in self.running_threads and self.running_threads[script_path].isRunning():
                self.execution_mutex.unlock()
                QMessageBox.warning(self, "Already Running", f"Script '{os.path.basename(script_path)}' is already running.")
                continue
            self.execution_mutex.unlock()
            
            # Check if file exists
            if not os.path.exists(script_path):
                QMessageBox.warning(self, "File Not Found", f"Script file not found: {script_path}")
                continue
            
            # Prompt for custom parameters
            custom_args, ok = QInputDialog.getText(
                self, 'Script Parameters', 
                f"Script: {script.get('description', os.path.basename(script_path))}\n"
                f"Default: {script.get('parameters', '(none)')}\n\n"
                f"Enter additional parameters (optional):"
            )
            
            if not ok:
                continue
                
            full_args = f"{script.get('parameters', '')} {custom_args}".strip()
            
            # Create and start execution thread
            thread = ScriptExecutionThread(script, full_args, self.emitter, self)
            self.execution_mutex.lock()
            self.running_threads[script_path] = thread
            self.execution_mutex.unlock()
            
            # Fix the lambda closure issue by using default parameter
            thread.finished.connect(lambda path=script_path: self.cleanup_thread(path))
            thread.start()
            self.update_control_button_states() # Update button states immediately

    def cleanup_thread(self, script_path):
        self.execution_mutex.lock()
        if script_path in self.running_threads:
            del self.running_threads[script_path]
        self.execution_mutex.unlock()
        self.update_control_button_states()

    def pause_execution(self):
        self.execution_mutex.lock()
        threads_to_pause = [thread for thread in self.running_threads.values() if thread.isRunning() and not thread.is_paused]
        self.execution_mutex.unlock()

        if not threads_to_pause:
            QMessageBox.information(self, "No Running Scripts", "No running scripts to pause.")
            return

        for thread in threads_to_pause:
            thread.pause()
        self.emitter.output_signal.emit("[PAUSED] Attempting to pause all running script executions.\n", "orange")
        self.update_control_button_states()

    def resume_execution(self):
        self.execution_mutex.lock()
        threads_to_resume = [thread for thread in self.running_threads.values() if thread.isRunning() and thread.is_paused]
        self.execution_mutex.unlock()

        if not threads_to_resume:
            QMessageBox.information(self, "No Paused Scripts", "No paused scripts to resume.")
            return

        for thread in threads_to_resume:
            thread.resume()
        self.emitter.output_signal.emit("[RESUMED] Attempting to resume all paused script executions.\n", "green")
        self.update_control_button_states()

    def stop_execution(self):
        self.execution_mutex.lock()
        threads_to_stop = list(self.running_threads.values()) # Create a copy for iteration
        self.execution_mutex.unlock()

        if not threads_to_stop:
            QMessageBox.information(self, "No Running Scripts", "No scripts are currently running.")
            return

        confirm = QMessageBox.question(
            self, "Confirm Stop",
            f"Are you sure you want to stop all {len(threads_to_stop)} running script(s)?",
            QMessageBox.Yes | QMessageBox.No
        )
        if confirm == QMessageBox.Yes:
            self.emitter.output_signal.emit("[STOPPING] Stopping all script executions...\n", "red")
            for thread in threads_to_stop:
                thread.stop()
            # Cleanup will happen via signals, but disable buttons immediately
            self.update_control_button_states()

    def update_control_button_states(self):
        self.execution_mutex.lock()
        running_threads = list(self.running_threads.values())
        self.execution_mutex.unlock()

        any_running_not_paused = any(t.isRunning() and not t.is_paused for t in running_threads)
        any_paused = any(t.isRunning() and t.is_paused for t in running_threads)
        any_running = any(t.isRunning() for t in running_threads)

        self.pause_btn.setEnabled(any_running_not_paused)
        self.resume_btn.setEnabled(any_paused)
        self.stop_btn.setEnabled(any_running)

    def export_logs_csv(self):
        if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
            QMessageBox.information(self, "Logs", "No logs available to export.")
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export Logs as CSV", "logs.csv", "CSV Files (*.csv)")
        if path:
            try:
                with open(LOG_FILE, 'r', encoding='utf-8') as log_file, open(path, 'w', newline='', encoding='utf-8') as csvfile:
                    writer = csv.writer(csvfile)
                    writer.writerow(["Timestamp", "Script Path", "Exit Code", "Error"]) # Updated header

                    for line in log_file:
                        try:
                            log_entry = json.loads(line.strip())
                            writer.writerow([
                                log_entry.get('timestamp', 'N/A'),
                                log_entry.get('script', 'N/A'),
                                log_entry.get('exit_code', 'N/A'),
                                log_entry.get('error', 'None')
                            ])
                        except json.JSONDecodeError:
                            # Handle old log format or malformed lines if necessary
                            self.emitter.output_signal.emit(f"[WARNING] Skipped malformed log line during CSV export: {line.strip()}\n", "orange")
                            continue
                                
                QMessageBox.information(self, "Success", f"Logs exported to {path}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Failed to export logs: {str(e)}")

    def view_logs(self):
        if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
            QMessageBox.information(self, "Logs", "No logs found yet.")
            return
            
        tag_filter, ok = QInputDialog.getText(self, "Filter Logs", "Enter script name or keyword to filter (optional):")
        
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                raw_logs = f.readlines()
        except Exception as e:
            QMessageBox.critical(self, "Log Error", f"Failed to read logs: {str(e)}")
            return
            
        filtered_entries = []
        for line in raw_logs:
            try:
                entry = json.loads(line.strip())
                # Flatten the entry for easier search
                entry_str = json.dumps(entry).lower() 
                if ok and tag_filter and tag_filter.lower() not in entry_str:
                    continue
                filtered_entries.append(entry)
            except json.JSONDecodeError:
                # Handle old log format or malformed lines
                self.emitter.output_signal.emit(f"[WARNING] Skipped malformed log line in view logs: {line.strip()}\n", "orange")
                continue
            
        # Show last 100 entries, formatted nicely
        display_logs_formatted = []
        for entry in filtered_entries[-100:]:
            status_color = "white"
            if entry.get('exit_code') == 0:
                status_color = "green"
            elif entry.get('exit_code') == -1 or "error" in entry.get('error', '').lower():
                status_color = "red"

            display_logs_formatted.append(
                f'<span style="color:#ADADAD;">[{entry.get("timestamp", "N/A")}]</span> '
                f'Script: <span style="color:#87CEEB;">{os.path.basename(entry.get("script", "N/A"))}</span>, '
                f'Exit Code: <span style="color:{status_color};">{entry.get("exit_code", "N/A")}</span>, '
                f'Error: <span style="color:orange;">{entry.get("error", "None")}</span>'
            )
        
        log_text_html = "<br>".join(display_logs_formatted)
        
        # Create a dialog to show logs
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Script Execution Logs")
        dialog.setText(f"Showing last {len(display_logs_formatted)} entries:")
        
        # Using a QTextEdit in QMessageBox for rich text
        text_edit = QTextEdit()
        text_edit.setReadOnly(True)
        text_edit.setFont(QFont("Consolas", 9))
        text_edit.setHtml(log_text_html)
        text_edit.setMinimumSize(700, 400) # Give it a decent size
        
        # Set text_edit as the "detailed text" which PyQt renders as a scrollable area
        dialog.setDetailedText(text_edit.toPlainText()) # Still need to set plain text here for the detailed text button to appear
        dialog.findChild(QTextEdit).setHtml(log_text_html) # But then force HTML content into the actual QTextEdit

        dialog.exec_()


    def schedule_script(self):
        rows = sorted(set(index.row() for index in self.table.selectedIndexes()))
        if not rows:
            QMessageBox.warning(self, "No Selection", "Please select a script to schedule.")
            return
        if len(rows) > 1:
            QMessageBox.warning(self, "Multiple Selection", "Please select only one script to schedule at a time.")
            return

        script = self.script_list[rows[0]]

        schedule_type, ok_type = QInputDialog.getItem(
            self, 'Schedule Type', 'Choose schedule type:',
            ['Interval', 'Date/Time'], 0, False
        )

        if not ok_type:
            return

        if schedule_type == 'Interval':
            interval, ok_interval = QInputDialog.getInt(
                self, 'Schedule Interval', 
                'Run every N seconds:\n(Minimum: 10 seconds for testing, 60 seconds recommended for production)', 
                min=10, max=86400, value=300
            )
            if not ok_interval:
                return
            
            try:
                job_id = f"scheduled_interval_{script['type']}_{os.path.basename(script['path'])}_{datetime.now().timestamp()}"
                self.scheduler.add_job(
                    'script_runner_gui:run_scheduled_script',
                    'interval',
                    args=[script],
                    seconds=interval,
                    id=job_id,
                    replace_existing=True
                )
                QMessageBox.information(self, "Scheduled", f"Script scheduled to run every {interval} seconds. Job ID: {job_id}")
            except Exception as e:
                QMessageBox.critical(self, "Schedule Error", f"Failed to schedule script: {str(e)}")

        elif schedule_type == 'Date/Time':
            dialog = QDialog(self)
            dialog.setWindowTitle("Schedule by Date/Time")
            form_layout = QFormLayout(dialog)

            datetime_edit = QDateTimeEdit(QDateTime.currentDateTime().addSecs(60)) # Default to 1 min in future
            datetime_edit.setCalendarPopup(True)
            datetime_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            form_layout.addRow("Run at:", datetime_edit)

            run_once_cb = QComboBox()
            run_once_cb.addItems(["Run Once", "Repeat Daily", "Repeat Weekly"])
            form_layout.addRow("Repetition:", run_once_cb)

            ok_cancel_buttons = QHBoxLayout()
            ok_button = QPushButton("OK")
            ok_button.clicked.connect(dialog.accept)
            cancel_button = QPushButton("Cancel")
            cancel_button.clicked.connect(dialog.reject)
            ok_cancel_buttons.addStretch()
            ok_cancel_buttons.addWidget(ok_button)
            ok_cancel_buttons.addWidget(cancel_button)
            form_layout.addRow(ok_cancel_buttons)

            if dialog.exec_() == QDialog.Accepted:
                run_datetime = datetime_edit.dateTime().toPyDateTime()
                repeat_type = run_once_cb.currentText()

                job_id = f"scheduled_datetime_{script['type']}_{os.path.basename(script['path'])}_{datetime.now().timestamp()}"
                try:
                    if repeat_type == "Run Once":
                        self.scheduler.add_job(
                            'script_runner_gui:run_scheduled_script',
                            'date',
                            args=[script],
                            run_date=run_datetime,
                            id=job_id
                        )
                        QMessageBox.information(self, "Scheduled", f"Script scheduled to run once at {run_datetime.strftime('%Y-%m-%d %H:%M:%S')}. Job ID: {job_id}")
                    elif repeat_type == "Repeat Daily":
                        self.scheduler.add_job(
                            'script_runner_gui:run_scheduled_script',
                            'cron',
                            args=[script],
                            hour=run_datetime.hour,
                            minute=run_datetime.minute,
                            second=run_datetime.second,
                            id=job_id
                        )
                        QMessageBox.information(self, "Scheduled", f"Script scheduled to repeat daily at {run_datetime.strftime('%H:%M:%S')}. Job ID: {job_id}")
                    elif repeat_type == "Repeat Weekly":
                        # APScheduler 'cron' trigger uses day_of_week (0=Mon, 6=Sun)
                        # datetime.weekday() returns 0=Mon, 6=Sun
                        self.scheduler.add_job(
                            'script_runner_gui:run_scheduled_script',
                            'cron',
                            args=[script],
                            day_of_week=run_datetime.weekday(),
                            hour=run_datetime.hour,
                            minute=run_datetime.minute,
                            second=run_datetime.second,
                            id=job_id
                        )
                        QMessageBox.information(self, "Scheduled", f"Script scheduled to repeat weekly on {run_datetime.strftime('%A')} at {run_datetime.strftime('%H:%M:%S')}. Job ID: {job_id}")
                except Exception as e:
                    QMessageBox.critical(self, "Schedule Error", f"Failed to schedule script: {str(e)}")

    def manage_schedules(self):
        dialog = ManageSchedulesDialog(self, self.scheduler)
        dialog.exec_()

    def run_script_scheduled(self, script):
        """Run script from scheduler - non-blocking"""
        script_path = script['path']
        self.execution_mutex.lock()
        is_running = script_path in self.running_threads and self.running_threads[script_path].isRunning()
        self.execution_mutex.unlock()

        if is_running:
            self.emitter.output_signal.emit(f"[INFO] Scheduled script '{os.path.basename(script_path)}' skipped as it's already running.\n", "orange")
            return
        
        # Check if file exists for scheduled runs
        if not os.path.exists(script_path):
            self.emitter.output_signal.emit(f"[ERROR] Scheduled script file not found: {script_path}. Please update script path or remove schedule.\n", "red")
            self.write_log(script_path, -1, "Scheduled script file not found.")
            return

        thread = ScriptExecutionThread(script, script.get('parameters', ''), self.emitter, self)
        self.execution_mutex.lock()
        self.running_threads[script_path] = thread
        self.execution_mutex.unlock()
        
        thread.finished.connect(lambda path=script_path: self.cleanup_thread(path))
        thread.start()
        self.emitter.output_signal.emit(f"[SCHEDULED RUN] Initiating scheduled run for '{os.path.basename(script_path)}'.\n", "blue")
        self.update_control_button_states()

    def export_scripts(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Scripts", "scripts_export.json", "JSON Files (*.json)")
        if path:
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(self.script_list, f, indent=4, ensure_ascii=False)
                QMessageBox.information(self, "Success", f"Scripts exported to {path}")
            except Exception as e:
                QMessageBox.critical(self, "Export Error", f"Failed to export scripts: {str(e)}")

    def import_scripts(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Scripts", "", "JSON Files (*.json)")
        if path:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    imported = json.load(f)
                    
                if not isinstance(imported, list):
                    QMessageBox.warning(self, "Import Error", "Invalid file format. Expected a list of scripts.")
                    return
                
                # Filter out duplicates based on path before extending
                unique_imported = []
                existing_paths = {s['path'] for s in self.script_list}
                for script in imported:
                    if 'path' in script and script['path'] not in existing_paths:
                        unique_imported.append(script)
                        existing_paths.add(script['path']) # Add to set to catch duplicates within the import file itself
                    else:
                        self.emitter.output_signal.emit(f"[WARNING] Skipped duplicate script on import: {script.get('path', 'N/A')}\n", "orange")
                        
                if unique_imported:
                    self.script_list.extend(unique_imported)
                    self.save_scripts()
                    self.load_table()
                    QMessageBox.information(self, "Success", f"Imported {len(unique_imported)} unique scripts successfully. {len(imported) - len(unique_imported)} duplicates skipped.")
                else:
                    QMessageBox.information(self, "Import", "No new unique scripts found to import.")

            except Exception as e:
                QMessageBox.critical(self, "Import Error", f"Failed to import scripts: {str(e)}")

    def append_disclaimer(self):
        disclaimer = (
            "\n[DISCLAIMER] This tool executes scripts on your local system. "
            "Please ensure you trust and understand all scripts before execution. "
            "Always run in a safe and secure environment. Use at your own risk.\n"
        )
        self.emitter.output_signal.emit(disclaimer, "orange")

    def closeEvent(self, event):
        """Handle application shutdown"""
        # Stop all running threads
        self.emitter.output_signal.emit("\n[SHUTDOWN] Stopping all active script executions...\n", "red")
        self.execution_mutex.lock()
        threads_to_stop_copy = list(self.running_threads.values())
        self.execution_mutex.unlock()

        for thread in threads_to_stop_copy:
            if thread.isRunning():
                thread.stop()
                thread.wait(2000)  # Wait up to 2 seconds for each to terminate
        
        # Shutdown scheduler (important for persistent jobs)
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=True) # Wait for jobs to finish gracefully
                self.emitter.output_signal.emit("[SHUTDOWN] APScheduler shut down successfully.\n", "blue")
        except Exception as e:
            self.emitter.output_signal.emit(f"[SHUTDOWN ERROR] Failed to gracefully shut down scheduler: {str(e)}\n", "red")
            # If graceful fails, try forceful
            try:
                self.scheduler.shutdown(wait=False)
            except:
                pass
        
        # Save config
        self.save_scripts()
        self.save_interpreter_settings()
        
        event.accept()

if __name__ == "__main__":
    # Enable high DPI support
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Modern look
    
    # Set application metadata
    app.setApplicationName("Script Runner")
    app.setApplicationVersion("2.1") # Updated version for new languages
    app.setOrganizationName("SmaRTy Saini")
    
    window = ScriptRunner()
    window.show()
    
    sys.exit(app.exec_())