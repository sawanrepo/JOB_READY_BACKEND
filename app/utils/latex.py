import os
import subprocess
import uuid
from jinja2 import Environment, FileSystemLoader
from fastapi import HTTPException

def render_latex_template(content: str) -> str:
    env = Environment(
        loader=FileSystemLoader("app/templates"),
        block_start_string='\\BLOCK{',
        block_end_string='}',
        variable_start_string='\\VAR{',
        variable_end_string='}',
        comment_start_string='\\#{',
        comment_end_string='}',
        trim_blocks=True,
        autoescape=False
    )
    
    template = env.get_template("resume.tex.j2")
    return template.render(content=content)

def compile_latex_to_pdf(tex_content: str, output_dir: str = "output") -> str:
    os.makedirs(output_dir, exist_ok=True)
    filename = f"resume_{uuid.uuid4()}"
    tex_path = os.path.join(output_dir, f"{filename}.tex")
    pdf_path = os.path.join(output_dir, f"{filename}.pdf")
    
    # Write LaTeX file
    with open(tex_path, "w") as f:
        f.write(tex_content)
    
    # Compile to PDF
    result = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", f"-output-directory={output_dir}", tex_path],
        capture_output=True,
        text=True
    )
    
    # Clean up auxiliary files
    aux_files = [f"{filename}.aux", f"{filename}.log", f"{filename}.out"]
    for file in aux_files:
        file_path = os.path.join(output_dir, file)
        if os.path.exists(file_path):
            os.remove(file_path)
    
    if result.returncode != 0 or not os.path.exists(pdf_path):
        error_msg = f"LaTeX compilation failed: {result.stderr}"
        if os.path.exists(tex_path):
            os.remove(tex_path)
        raise HTTPException(status_code=500, detail=error_msg)
    
    os.remove(tex_path)
    return pdf_path