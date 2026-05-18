import os
import subprocess
import uuid
import re
import shutil
from jinja2 import Environment, FileSystemLoader
from fastapi import HTTPException
from app.config import settings

# Get the project root directory
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

def escape_latex_deep(value):
    if isinstance(value, str):
        replacements = {
            '&': r'\&',
            '%': r'\%',
            '$': r'\$',
            '#': r'\#',
            '_': r'\_',
            '{': r'\{',
            '}': r'\}',
            '~': r'\textasciitilde{}',
            '^': r'\^{}',
            '\\': r'\textbackslash{}',
            '<': r'\textless{}',
            '>': r'\textgreater{}',
            '|': r'\textbar{}',
        }
        regex = re.compile('|'.join(re.escape(k) for k in replacements))
        value =  regex.sub(lambda m: replacements[m.group()], value)
        value = value.replace('--', '-{}-')
        return value

    elif isinstance(value, dict):
        escaped_dict = {}
        for k, v in value.items():
            escaped_key = escape_latex_deep(k)
            escaped_value = escape_latex_deep(v)
            escaped_dict[escaped_key] = escaped_value
        return escaped_dict
    
    elif isinstance(value, list):
        return [escape_latex_deep(item) for item in value]
    
    else:
        return value

def render_latex_template(content: dict) -> str:
    print("[DEBUG] Rendering LaTeX template with content:", content)
    content = escape_latex_deep(content)
    print("[DEBUG] Escaped content for LaTeX:", content)

    templates_dir = os.path.join(PROJECT_ROOT, "app", "templates")

    env = Environment(
        loader=FileSystemLoader(templates_dir),
        block_start_string='\\BLOCK{',
        block_end_string='}',
        variable_start_string='\\VAR{',
        variable_end_string='}',
        comment_start_string='\\#{',
        comment_end_string='}',
        trim_blocks=True,
        autoescape=False
    )

    try:
        template = env.get_template("resume.tex.j2")
        rendered = template.render(content=content)
        return rendered
    except Exception as e:
        print(f"[ERROR] Template rendering failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Template rendering failed: {str(e)}")

def compile_latex_to_pdf(tex_content: str, output_dir: str = "output", content: dict = None) -> str:
    os.makedirs(output_dir, exist_ok=True)
    filename = f"resume_{uuid.uuid4()}"
    pdf_path = os.path.join(output_dir, f"{filename}.pdf")
    
    # Create a temporary working directory
    work_dir = os.path.join(output_dir, f"work_{filename}")
    os.makedirs(work_dir, exist_ok=True)
    templates_dir = os.path.join(PROJECT_ROOT, "app", "templates")
    
    try:
        # Define paths
        tex_filename = f"{filename}.tex"
        tex_work_path = os.path.join(work_dir, tex_filename)
        log_work_path = os.path.join(work_dir, f"{filename}.log")
        
        # Copy all dependencies to working directory
        altacv_src = os.path.join(templates_dir, "altacv")
        altacv_dest = os.path.join(work_dir, "altacv")
        shutil.copytree(altacv_src, altacv_dest, dirs_exist_ok=True)
        
        # Verify files were copied
        if not os.path.exists(os.path.join(altacv_dest, "altacv.cls")):
            raise FileNotFoundError("altacv.cls not found in working directory")
        
        latex_command = settings.LATEX_PATH
        
        # Presets definition
        preset_standard = {"fontsize": "10pt", "margin": "0.7in", "itemsep": "3pt", "sectionspace": "0.5em"}
        preset_tightest = {"fontsize": "10pt", "margin": "0.5in", "itemsep": "1pt", "sectionspace": "0.3em"}
        
        selected_preset = preset_standard
        current_tex_content = tex_content
        
        if content:
            print("[DEBUG] Smart spacing: Compiling with standard preset (Pass 1)...")
            content_std = dict(content)
            content_std.update(preset_standard)
            tex_std = render_latex_template(content_std)
            
            with open(tex_work_path, "w", encoding="utf-8") as f:
                f.write(tex_std)
                
            result = subprocess.run(
                [latex_command, "-interaction=nonstopmode", tex_filename],
                cwd=work_dir,
                capture_output=True,
                text=True,
            )
            
            pages_std = 1
            if os.path.exists(log_work_path):
                with open(log_work_path, "r", errors="ignore") as lf:
                    log_data = lf.read()
                page_match = re.search(r"Output written on .*?\.pdf \((\d+) pages?", log_data)
                if page_match:
                    pages_std = int(page_match.group(1))
            print(f"[DEBUG] Standard preset resulted in {pages_std} page(s)")
            
            if pages_std == 2:
                print("[DEBUG] Smart spacing: Attempting to squeeze 2 pages to 1 page...")
                content_tight = dict(content)
                content_tight.update(preset_tightest)
                tex_tight = render_latex_template(content_tight)
                
                with open(tex_work_path, "w", encoding="utf-8") as f:
                    f.write(tex_tight)
                    
                result = subprocess.run(
                    [latex_command, "-interaction=nonstopmode", tex_filename],
                    cwd=work_dir,
                    capture_output=True,
                    text=True,
                )
                
                pages_tight = 2
                if os.path.exists(log_work_path):
                    with open(log_work_path, "r", errors="ignore") as lf:
                        log_data = lf.read()
                    page_match = re.search(r"Output written on .*?\.pdf \((\d+) pages?", log_data)
                    if page_match:
                        pages_tight = int(page_match.group(1))
                print(f"[DEBUG] Tightest preset resulted in {pages_tight} page(s)")
                
                if pages_tight == 1:
                    print("[DEBUG] Smart spacing: Successfully squeezed to 1 page!")
                    selected_preset = preset_tightest
                    current_tex_content = tex_tight
                else:
                    print("[DEBUG] Smart spacing: Genuine 2-page resume. Keeping standard preset.")
                    selected_preset = preset_standard
                    current_tex_content = tex_std
                    
            elif pages_std >= 3:
                print("[DEBUG] Smart spacing: Attempting to squeeze 3+ pages to 2 pages...")
                content_tight = dict(content)
                content_tight.update(preset_tightest)
                tex_tight = render_latex_template(content_tight)
                
                with open(tex_work_path, "w", encoding="utf-8") as f:
                    f.write(tex_tight)
                    
                result = subprocess.run(
                    [latex_command, "-interaction=nonstopmode", tex_filename],
                    cwd=work_dir,
                    capture_output=True,
                    text=True,
                )
                
                pages_tight = 3
                if os.path.exists(log_work_path):
                    with open(log_work_path, "r", errors="ignore") as lf:
                        log_data = lf.read()
                    page_match = re.search(r"Output written on .*?\.pdf \((\d+) pages?", log_data)
                    if page_match:
                        pages_tight = int(page_match.group(1))
                print(f"[DEBUG] Tightest preset resulted in {pages_tight} page(s)")
                
                if pages_tight <= 2:
                    print(f"[DEBUG] Smart spacing: Successfully squeezed to {pages_tight} page(s)!")
                    selected_preset = preset_tightest
                    current_tex_content = tex_tight
                else:
                    print("[DEBUG] Smart spacing: Keeping standard preset.")
                    selected_preset = preset_standard
                    current_tex_content = tex_std
            else:
                selected_preset = preset_standard
                current_tex_content = tex_std
        else:
            selected_preset = None
            current_tex_content = tex_content

        # Write final selected LaTeX source and run second pass to resolve references/page bounds
        with open(tex_work_path, "w", encoding="utf-8") as f:
            f.write(current_tex_content)
            
        # DEBUG: Save a copy of the LaTeX content
        debug_tex_path = os.path.join(output_dir, f"{filename}.tex")
        with open(debug_tex_path, "w", encoding="utf-8") as f:
            f.write(current_tex_content)
        print(f"[DEBUG] Saved final LaTeX file at: {debug_tex_path}")
            
        print("[DEBUG] Running final LaTeX pass...")
        last_result = subprocess.run(
            [latex_command, "-interaction=nonstopmode", tex_filename],
            cwd=work_dir,
            capture_output=True,
            text=True,
        )
        
        # Check if PDF was generated
        pdf_work_path = os.path.join(work_dir, f"{filename}.pdf")
        
        # Check for compilation errors
        if last_result and last_result.returncode != 0:
            error_detail = f"LaTeX compilation failed with exit code {last_result.returncode}"
            # Save logs for debugging
            with open(os.path.join(work_dir, "latex_stdout.log"), "w") as f:
                f.write(last_result.stdout)
            with open(os.path.join(work_dir, "latex_stderr.log"), "w") as f:
                f.write(last_result.stderr)
            raise RuntimeError(f"{error_detail}\nSTDERR: {last_result.stderr[:500]}")
        
        # Verify PDF exists and has content
        if not os.path.exists(pdf_work_path):
            raise FileNotFoundError(f"PDF file not generated at {pdf_work_path}")
        
        if os.path.getsize(pdf_work_path) < 1024:  # Less than 1KB is likely empty
            raise ValueError(f"PDF file is too small: {os.path.getsize(pdf_work_path)} bytes")

        # Move PDF to final output location
        shutil.move(pdf_work_path, pdf_path)
        print(f"[DEBUG] PDF compiled successfully at: {pdf_path}")
        return pdf_path
        
    except Exception as e:
        # Preserve working directory for debugging
        print(f"[ERROR] Compilation failed: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=(
                f"Compilation failed: {str(e)}\n"
                f"Debug files preserved at: {work_dir}\n"
                f"LaTeX source saved at: {debug_tex_path}"
            )
        )
    
    finally:
        # Only clean up if compilation was successful
        if 'pdf_path' in locals() and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1024:
            print(f"[DEBUG] Cleaning up working directory: {work_dir}")
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"[WARNING] Preserving working directory due to errors: {work_dir}")