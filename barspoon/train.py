#!/usr/bin/env python3
import argparse
import os
import shutil
from pathlib import Path
from typing import Iterable, Sequence, Tuple

import numpy as np
import numpy.typing as npt
import pandas as pd
import pytorch_lightning as pl
import torch
from pytorch_lightning.callbacks import EarlyStopping, ModelCheckpoint
from pytorch_lightning.loggers import CSVLogger
from sklearn.model_selection import train_test_split
from torch.utils.data import DataLoader

from .data import BagDataset
from .model import LitEncDecTransformer
from .utils import make_dataset_df, make_preds_df


def main():
    parser = make_argument_parser()
    args = parser.parse_args()

    if (args.output_dir / "done").exists():
        # already done...
        return
    elif args.output_dir.exists():
        # previous attempt didn't finish; start over
        shutil.rmtree(args.output_dir)

    pl.seed_everything(0)
    torch.set_float32_matmul_precision("medium")

    # read target labels from file, if need be
    if args.target_labels:
        target_labels = args.target_labels
    else:
        with open(args.target_file) as f:
            target_labels = [l.strip() for l in f if l]

    dataset_df = make_dataset_df(
        clini_tables=args.clini_tables,
        slide_tables=args.slide_tables,
        feature_dirs=args.feature_dirs,
        patient_col=args.patient_col,
        slide_col=args.slide_col,
        group_by=args.group_by,
        target_labels=target_labels,
    )

    # see if target labels are good, otherwise die a fiery death
    target_labels = np.array(target_labels)
    assert_targets_are_sane(dataset_df=dataset_df, target_labels=target_labels)

    pos_weight = get_pos_weight(
        torch.tensor(
            dataset_df[target_labels].apply(pd.to_numeric).values, dtype=torch.float32
        )
    )

    train_items, valid_items = train_test_split(dataset_df.index, test_size=0.2)
    train_df = dataset_df.loc[train_items]
    valid_df = dataset_df.loc[valid_items]

    train_dl, valid_dl = make_dataloaders(
        train_bags=train_df.path.values,
        train_targets=torch.tensor(train_df[target_labels].apply(pd.to_numeric).values),  # type: ignore
        valid_bags=valid_df.path.values,
        valid_targets=torch.tensor(valid_df[target_labels].apply(pd.to_numeric).values),  # type: ignore
        instances_per_bag=args.instances_per_bag,
        batch_size=args.batch_size,
        num_workers=args.num_workers,
    )

    example_bags, _ = next(iter(train_dl))
    d_features = example_bags.size(-1)

    model = LitEncDecTransformer(
        d_features=d_features,
        target_labels=target_labels,
        pos_weight=pos_weight,
        # other hparams
        training_set=list(train_items),
        validation_set=list(valid_items),
        **{k: v for k, v in vars(args).items() if k not in {"target_labels"}},
    )

    trainer = pl.Trainer(
        default_root_dir=args.output_dir,
        callbacks=[
            EarlyStopping(
                monitor="val_TopKMultilabelAUROC", mode="max", patience=args.patience
            ),
            ModelCheckpoint(
                monitor="val_TopKMultilabelAUROC",
                mode="max",
                filename="checkpoint-{epoch:02d}-{val_TopKMultilabelAUROC:0.3f}",
            ),
        ],
        max_epochs=args.max_epochs,
        accelerator="auto",
        accumulate_grad_batches=args.accumulate_grad_samples // args.batch_size,
        gradient_clip_val=0.5,
        logger=CSVLogger(save_dir=args.output_dir),
    )

    trainer.fit(model=model, train_dataloaders=train_dl, val_dataloaders=valid_dl)

    predictions = torch.cat(trainer.predict(model=model, dataloaders=valid_dl, return_predictions=True))  # type: ignore
    preds_df = make_preds_df(
        predictions=predictions,
        base_df=valid_df,
        target_labels=target_labels,
        loss=model.loss,
    )
    preds_df.to_csv(args.output_dir / "valid-patient-preds.csv")

    with open(args.output_dir / "done", "w"):
        pass


def make_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-o",
        "--output-dir",
        metavar="PATH",
        type=Path,
        required=True,
        help="Directory path for the output",
    )

    parser.add_argument(
        "-c",
        "--clini-table",
        metavar="PATH",
        dest="clini_tables",
        type=Path,
        required=True,
        action="append",
        help="Path to the clinical table. Can be specified multiple times",
    )
    parser.add_argument(
        "-s",
        "--slide-table",
        metavar="PATH",
        dest="slide_tables",
        type=Path,
        required=True,
        action="append",
        help="Path to the slide table. Can be specified multiple times",
    )
    parser.add_argument(
        "-f",
        "--feature-dir",
        metavar="PATH",
        dest="feature_dirs",
        type=Path,
        required=True,
        action="append",
        help="Path containing the slide features as `h5` files. Can be specified multiple times",
    )

    targets_parser = parser.add_mutually_exclusive_group(required=True)
    targets_parser.add_argument(
        "-t",
        "--target-label",
        metavar="LABEL",
        type=str,
        action="append",
        dest="target_labels",
        help="Target labels to train for. Can be specified multiple times",
    )
    targets_parser.add_argument(
        "--target-file",
        metavar="PATH",
        type=Path,
        help="A file containing a list of target labels, one per line.",
    )

    parser.add_argument(
        "--patient-col",
        metavar="COL",
        type=str,
        help="Name of the patient column",
    )
    parser.add_argument(
        "--slide-col",
        metavar="COL",
        type=str,
        help="Name of the slide column",
    )
    parser.add_argument(
        "--group-by",
        metavar="COL",
        type=str,
        help="How to group slides. If 'clini' table is given, default is 'patient'; otherwise, default is 'slide'",
    )

    model_parser = parser.add_argument_group("model options")
    model_parser.add_argument("--num-encoder-heads", type=int, default=8)
    model_parser.add_argument("--num-decoder-heads", type=int, default=8)
    model_parser.add_argument("--num-encoder-layers", type=int, default=2)
    model_parser.add_argument("--num-decoder-layers", type=int, default=2)
    model_parser.add_argument("--d-model", type=int, default=512)
    model_parser.add_argument("--dim-feedforward", type=int, default=2048)

    training_parser = parser.add_argument_group("training options")
    training_parser.add_argument("--instances-per-bag", type=int, default=2**12)
    training_parser.add_argument("--learning-rate", type=float, default=1e-4)
    training_parser.add_argument("--batch-size", type=int, default=4)
    training_parser.add_argument("--accumulate-grad-samples", type=int, default=32)
    training_parser.add_argument(
        "--num-workers", type=int, default=min(os.cpu_count() or 0, 8)
    )
    training_parser.add_argument("--patience", type=int, default=16)
    training_parser.add_argument("--max-epochs", type=int, default=256)

    return parser


def assert_targets_are_sane(
    dataset_df: pd.DataFrame, target_labels: npt.NDArray[np.str_]
) -> None:
    label_count = dataset_df[target_labels].nunique(dropna=True)
    assert (
        label_count == 2
    ).all(), f"the following labels have the wrong number of entries: {dict(label_count[label_count != 2])}"

    numeric_labels = (
        dataset_df[target_labels]
        .select_dtypes(["int16", "int32", "int64", "float16", "float32", "float64"])
        .columns.values
    )
    assert not (
        non_numeric_labels := set(target_labels) - set(numeric_labels)
    ), f"non-numeric labels: {non_numeric_labels}"


def get_pos_weight(targets: torch.Tensor) -> torch.Tensor:
    pos_samples = targets.nansum(dim=0)
    neg_samples = (1 - targets).nansum(dim=0)
    pos_weight = neg_samples / pos_samples
    return pos_weight


def make_dataloaders(
    *,
    train_bags: Sequence[Iterable[Path]],
    train_targets: torch.Tensor,
    valid_bags: Sequence[Iterable[Path]],
    valid_targets: torch.Tensor,
    batch_size: int,
    instances_per_bag: int,
    num_workers: int,
) -> Tuple[DataLoader, DataLoader]:
    train_ds = BagDataset(
        bags=train_bags,
        targets=train_targets,
        instances_per_bag=instances_per_bag,
        deterministic=False,
    )
    train_dl = DataLoader(
        train_ds, batch_size=batch_size, num_workers=num_workers, shuffle=True
    )

    valid_ds = BagDataset(
        bags=valid_bags,
        targets=valid_targets,
        instances_per_bag=instances_per_bag,
        deterministic=True,
    )
    valid_dl = DataLoader(valid_ds, batch_size=batch_size, num_workers=num_workers)

    return train_dl, valid_dl


if __name__ == "__main__":
    main()