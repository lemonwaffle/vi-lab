import argparse
from pathlib import Path

import pytorch_lightning as pl
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning.loggers import WandbLogger

import src.experiments as experiments
from src.experiments import MVAE_Tester
from src.utils import load_yaml


def main(hparams, args):
    pl.seed_everything(hparams["seed"])

    # Init logger
    wandb_logger = WandbLogger(
        name=hparams["name"],
        project=args.project_name,
        id=hparams["name"],  # For resuming
    )
    wandb_logger.log_hyperparams(hparams)

    # Init experiment
    expt = getattr(experiments, hparams["experiment"])(hparams)

    # ModelCheckpoint and EarlyStopping callbacks
    checkpoint_dir = Path("checkpoints") / hparams["name"]
    # FIXME Change this accordingly
    checkpoint_path = (
        checkpoint_dir
        / args.project_name
        / hparams["name"]
        / "checkpoints"
        / "last.ckpt"
    )

    model_checkpoint = ModelCheckpoint(
        mode="max",
        save_last=True,
        monitor="val_elbo",
        dirpath=checkpoint_dir,
    )
    early_stop = EarlyStopping(
        monitor="val_elbo",
        patience=hparams["earlystop_patience"],
        mode="max",
        verbose=True,
    )

    # Init trainer
    trainer = pl.Trainer(
        fast_dev_run=args.fast_dev_run,
        # default_root_dir=checkpoint_dir,
        resume_from_checkpoint=str(checkpoint_path) if args.resume else None,
        deterministic=True,
        benchmark=True,
        # FIXME Disabling early stopping
        # callbacks=expt.callbacks + [early_stop],
        callbacks=expt.callbacks + [model_checkpoint],
        gpus=[1],
        logger=wandb_logger,
        weights_summary="top",
        max_epochs=hparams["max_epochs"],
        terminate_on_nan=True,
        # val_check_interval=0.25,
        # auto_lr_find=True,
        # limit_val_batches=0.,
    )

    trainer.fit(expt, expt.datamodule)
    trainer.test(datamodule=expt.datamodule)

    # mvae_tester = MVAE_Tester(expt)
    # mvae_tester.evaluate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="VAE Experiment")
    parser.add_argument(
        "--config", "-c", help="path to the config file", default="configs/config.yaml"
    )
    parser.add_argument(
        "--project_name", help="name of wandb project", default="vae-expt-v2"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="whether to attempt to resume from the checkpoint directory",
    )
    parser.add_argument(
        "--fast_dev_run",
        action="store_true",
        help="whether to run fast_dev_run",
    )

    args = parser.parse_args()

    # Load config file
    hparams = load_yaml(args.config)

    main(hparams, args)
