import os
import subprocess
import uuid
import re
import shutil
import logging
from jinja2 import Environment, FileSystemLoader
from fastapi import HTTPException
from app.config import settings

# Get the project root directory
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
logger = logging.getLogger(__name__)


def _tail_text(value: str, max_chars: int = 4000) -> str:
    if not value:
        return ""
    return value[-max_chars:]


def _read_log_tail(path: str, max_chars: int = 4000) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return _tail_text(f.read(), max_chars)
    except Exception:
        return ""


def _run_latex(latex_command: str, tex_filename: str, work_dir: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [latex_command, "-interaction=nonstopmode", "-no-shell-escape", tex_filename],
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=settings.LATEX_COMPILE_TIMEOUT_SECONDS,
    )

def escape_latex_deep(value):
    if isinstance(value, str):
        unicode_replacements = {
            "\u00a0": " ",
            "\u200b": "",
            "\u200c": "",
            "\u200d": "",
            "\u2010": "-",
            "\u2011": "-",
            "\u2012": "-",
            "\u2013": "-",
            "\u2014": "-",
            "\u2018": "'",
            "\u2019": "'",
            "\u201c": '"',
            "\u201d": '"',
            "\u2022": "-",
            "\u2026": "...",
            "\u2192": "->",
            "\u2190": "<-",
            "\u2264": "<=",
            "\u2265": ">=",
            "\u00d7": "x",
        }
        for original, replacement in unicode_replacements.items():
            value = value.replace(original, replacement)

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
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
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
    content = escape_latex_deep(content)

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
        autoescape=False  # nosec B701 - HTML escaping is incorrect for LaTeX. Deep custom LaTeX escaping is performed beforehand.
    )

    try:
        template = env.get_template("resume.tex.j2")
        rendered = template.render(content=content)
        return rendered
    except Exception as e:
        logger.error("Template rendering failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Template rendering failed. Please try again.")

def compile_latex_to_pdf(tex_content: str, output_dir: str = "output", content: dict = None) -> str:
    os.makedirs(output_dir, exist_ok=True)
    filename = f"resume_{uuid.uuid4()}"
    pdf_path = os.path.join(output_dir, f"{filename}.pdf")
    
    # Create a temporary working directory
    work_dir = os.path.join(output_dir, f"work_{filename}")
    debug_tex_path = None
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
            logger.debug("Smart spacing: compiling with standard preset")
            content_std = dict(content)
            content_std.update(preset_standard)
            tex_std = render_latex_template(content_std)
            
            with open(tex_work_path, "w", encoding="utf-8") as f:
                f.write(tex_std)
                
            result = _run_latex(latex_command, tex_filename, work_dir)
            
            pages_std = 1
            if os.path.exists(log_work_path):
                with open(log_work_path, "r", errors="ignore") as lf:
                    log_data = lf.read()
                page_match = re.search(r"Output written on .*?\.pdf \((\d+) pages?", log_data)
                if page_match:
                    pages_std = int(page_match.group(1))
            logger.debug("Standard preset resulted in %s page(s)", pages_std)
            
            if pages_std == 2:
                logger.debug("Smart spacing: attempting to squeeze 2 pages to 1 page")
                content_tight = dict(content)
                content_tight.update(preset_tightest)
                tex_tight = render_latex_template(content_tight)
                
                with open(tex_work_path, "w", encoding="utf-8") as f:
                    f.write(tex_tight)
                    
                result = _run_latex(latex_command, tex_filename, work_dir)
                
                pages_tight = 2
                if os.path.exists(log_work_path):
                    with open(log_work_path, "r", errors="ignore") as lf:
                        log_data = lf.read()
                    page_match = re.search(r"Output written on .*?\.pdf \((\d+) pages?", log_data)
                    if page_match:
                        pages_tight = int(page_match.group(1))
                logger.debug("Tightest preset resulted in %s page(s)", pages_tight)
                
                if pages_tight == 1:
                    logger.debug("Smart spacing: successfully squeezed to 1 page")
                    selected_preset = preset_tightest
                    current_tex_content = tex_tight
                else:
                    logger.debug("Smart spacing: genuine 2-page resume, keeping standard preset")
                    selected_preset = preset_standard
                    current_tex_content = tex_std
                    
            elif pages_std >= 3:
                logger.debug("Smart spacing: attempting to squeeze 3+ pages to 2 pages")
                content_tight = dict(content)
                content_tight.update(preset_tightest)
                tex_tight = render_latex_template(content_tight)
                
                with open(tex_work_path, "w", encoding="utf-8") as f:
                    f.write(tex_tight)
                    
                result = _run_latex(latex_command, tex_filename, work_dir)
                
                pages_tight = 3
                if os.path.exists(log_work_path):
                    with open(log_work_path, "r", errors="ignore") as lf:
                        log_data = lf.read()
                    page_match = re.search(r"Output written on .*?\.pdf \((\d+) pages?", log_data)
                    if page_match:
                        pages_tight = int(page_match.group(1))
                logger.debug("Tightest preset resulted in %s page(s)", pages_tight)
                
                if pages_tight <= 2:
                    logger.debug("Smart spacing: successfully squeezed to %s page(s)", pages_tight)
                    selected_preset = preset_tightest
                    current_tex_content = tex_tight
                else:
                    logger.debug("Smart spacing: keeping standard preset")
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
            
        if settings.DEBUG:
            debug_tex_path = os.path.join(output_dir, f"{filename}.tex")
            with open(debug_tex_path, "w", encoding="utf-8") as f:
                f.write(current_tex_content)
            logger.debug("Saved temporary LaTeX file at: %s", debug_tex_path)
            
        logger.debug("Running final LaTeX pass")
        last_result = _run_latex(latex_command, tex_filename, work_dir)
        
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
            logger.error(
                "LaTeX compiler error detail for %s\nstdout tail:\n%s\nstderr tail:\n%s\nlatex log tail:\n%s",
                work_dir,
                _tail_text(last_result.stdout),
                _tail_text(last_result.stderr),
                _read_log_tail(log_work_path),
            )
            raise RuntimeError(error_detail)
        
        # Verify PDF exists and has content
        if not os.path.exists(pdf_work_path):
            raise FileNotFoundError(f"PDF file not generated at {pdf_work_path}")
        
        if os.path.getsize(pdf_work_path) < 1024:  # Less than 1KB is likely empty
            raise ValueError(f"PDF file is too small: {os.path.getsize(pdf_work_path)} bytes")

        # Move PDF to final output location
        shutil.move(pdf_work_path, pdf_path)
        logger.debug("PDF compiled successfully at: %s", pdf_path)
        return pdf_path
        
    except Exception as e:
        # Preserve working directory for debugging
        logger.error("Compilation failed in %s: %s", work_dir, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Resume PDF compilation failed. Please try again.")
    
    finally:
        if 'pdf_path' in locals() and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1024:
            logger.debug("Cleaning up working directory: %s", work_dir)
            shutil.rmtree(work_dir, ignore_errors=True)
            if debug_tex_path and os.path.exists(debug_tex_path):
                os.remove(debug_tex_path)
        elif not settings.DEBUG:
            logger.debug("Cleaning up failed LaTeX working directory: %s", work_dir)
            shutil.rmtree(work_dir, ignore_errors=True)
            if debug_tex_path and os.path.exists(debug_tex_path):
                os.remove(debug_tex_path)
        else:
            logger.warning("Preserving working directory due to errors: %s", work_dir)
