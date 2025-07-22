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
        raise HTTPException(status_code=500, detail=f"Template rendering failed: {str(e)}")

def compile_latex_to_pdf(tex_content: str, output_dir: str = "output") -> str:
    os.makedirs(output_dir, exist_ok=True)
    filename = f"resume_{uuid.uuid4()}"
    pdf_path = os.path.join(output_dir, f"{filename}.pdf")
    
    # Create a temporary working directory
    work_dir = os.path.join(output_dir, f"work_{filename}")
    os.makedirs(work_dir, exist_ok=True)
    templates_dir = os.path.join(PROJECT_ROOT, "app", "templates")
    
    try:
        # Write LaTeX content to file
        tex_filename = f"{filename}.tex"
        tex_work_path = os.path.join(work_dir, tex_filename)
        
        with open(tex_work_path, "w", encoding="utf-8") as f:
            f.write(tex_content)
            
        # DEBUG: Save a copy of the LaTeX content
        debug_tex_path = os.path.join(output_dir, f"{filename}.tex")
        with open(debug_tex_path, "w", encoding="utf-8") as f:
            f.write(tex_content)
        print(f"[DEBUG] Saved LaTeX file at: {debug_tex_path}")
            
        # Copy all dependencies to working directory
        altacv_src = os.path.join(templates_dir, "altacv")
        altacv_dest = os.path.join(work_dir, "altacv")
        shutil.copytree(altacv_src, altacv_dest, dirs_exist_ok=True)
        
        # Verify files were copied
        if not os.path.exists(os.path.join(altacv_dest, "altacv.cls")):
            raise FileNotFoundError("altacv.cls not found in working directory")
        
        # Run LaTeX compilation
        latex_command = settings.LATEX_PATH
        
        # Run compilation twice to resolve references
        last_result = None
        for i in range(2):
            result = subprocess.run(
                [latex_command, "-interaction=nonstopmode", tex_filename],
                cwd=work_dir,
                capture_output=True,
                text=True,
            )
            last_result = result
            
            # If compilation fails, break immediately
            if result.returncode != 0:
                print(f"[ERROR] LaTeX compilation failed on pass #{i+1}")
                print(f"Exit code: {result.returncode}")
                print(f"STDOUT:\n{result.stdout}")
                print(f"STDERR:\n{result.stderr}")
                break

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