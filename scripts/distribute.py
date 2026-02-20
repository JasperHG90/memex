# type: ignore

# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "typer>=0.9.0",
#     "huggingface_hub>=1.4.1"
# ]
# ///

from typing import Annotated
import pathlib as plb

import typer

app = typer.Typer(
    name='distribute',
    no_args_is_help=True,
    help='Tools for distributing models to Hugging Face repositories',
)


def _validate_repository_id(value: str) -> str:
    if len(value.split('/')) != 2:
        raise typer.BadParameter("Repository ID must be in the format 'username/repo'")
    return value


@app.command()
def upload_model(
    model_path: Annotated[
        plb.Path,
        typer.Argument(
            help='Path to the model file (e.g., .onnx) to upload',
            dir_okay=True,
            file_okay=False,
            exists=True,
            readable=True,
            resolve_path=True,
        ),
    ],
    repository_id: Annotated[
        str,
        typer.Argument(
            help='Hugging Face repository ID (e.g., username/repo) to upload the model to',
            callback=_validate_repository_id,
        ),
    ],
    hf_token: Annotated[
        str,
        typer.Option(
            help='Hugging Face API token with write access to the repository', envvar='HF_TOKEN'
        ),
    ],
    model_card_path: Annotated[
        str,
        typer.Option(
            help='Path to the model card file (e.g., README.md) to upload alongside the model',
        ),
    ] = 'MODEL_CARD.md',
    private: Annotated[
        bool,
        typer.Option(
            help='Whether the Hugging Face repository should be private (default: True)',
            show_default=True,
        ),
    ] = True,
):
    """
    Upload a model file to a Hugging Face repository.
    """
    import shutil
    from huggingface_hub import HfApi

    api = HfApi()

    api.create_repo(repo_id=repository_id, token=hf_token, private=private, exist_ok=True)

    print('Copying model card to model directory...')
    model_card_path = model_path.parent / model_card_path
    if not model_card_path.exists():
        raise FileNotFoundError(f"Model card file '{model_card_path}' does not exist.")
    shutil.copy(model_card_path, model_path / 'README.md')

    print('Uploading model to Hugging Face repository...')
    api.upload_folder(
        repo_id=repository_id,
        folder_path=str(model_path),
        token=hf_token,
    )

    print(f"Model '{model_path.name}' successfully uploaded to '{repository_id}'.")


if __name__ == '__main__':
    app()
