import torch
import torchvision
import wandb
from pytorch_lightning import Callback


class MultimodalVAEImageSampler(Callback):
    def __init__(self, num_samples=64) -> None:
        """Generates images and logs to wandb

        Requirements:
            `pl_module.model` should have `sample` method implemented,
                z -> samples

        Parameters
        ----------
        num_samples : int, optional
            , by default 64, i.e. 8x8 grid
        """
        super().__init__()
        self.num_samples = num_samples

    def on_epoch_end(self, trainer, pl_module):
        with torch.no_grad():
            pl_module.eval()

            # VAE model
            model = pl_module.model
            # Get samples
            samples = model.sample(self.num_samples, mean=True)

            # Create grid for each modality
            for i, s in enumerate(samples):
                grid = torchvision.utils.make_grid(s, nrow=8)
                grid = grid.permute(1, 2, 0).cpu().numpy()

                # Log samples
                trainer.logger.experiment.log(
                    {f"samples_{i}": wandb.Image(grid)}, commit=False
                )

            # Cross reconstruction

        pl_module.train()
