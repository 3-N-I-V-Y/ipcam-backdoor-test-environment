from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a GRU or LSTM classifier on window feature sequences.",
    )
    parser.add_argument("--rnn-type", choices=("gru", "lstm"), default="gru")
    parser.add_argument("--train", required=True, type=Path, help="Training NPZ from build_gru_sequences.py")
    parser.add_argument("--test", required=True, type=Path, help="Test NPZ from build_gru_sequences.py")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/models/gru-scan-subtype"),
    )
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument("--num-layers", type=int, default=1)
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--weight-decay", type=float, default=0.0001)
    parser.add_argument("--class-weight", choices=("none", "balanced"), default="balanced")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--seed", type=int, default=13)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import numpy as np
        import torch
    except ImportError as exc:
        raise SystemExit(
            "missing RNN dependency. Install the ML requirements first, for example: "
            "pip install -r requirements-ml.txt"
        ) from exc

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    train_payload = load_npz(np, args.train)
    test_payload = load_npz(np, args.test)
    x_train = train_payload["X"]
    y_train = train_payload["y"]
    x_test = test_payload["X"]
    y_test = test_payload["y"]
    classes = [str(value) for value in train_payload["classes"].tolist()]
    feature_columns = [str(value) for value in train_payload["feature_columns"].tolist()]

    if x_train.shape[0] == 0:
        raise SystemExit(f"no training sequences in {args.train}")
    if x_test.shape[0] == 0:
        raise SystemExit(f"no test sequences in {args.test}")
    if len(classes) < 2:
        raise SystemExit(f"need at least two target classes, got: {classes}")

    mean, std = feature_stats(np, x_train)
    x_train = normalize(x_train, mean, std)
    x_test = normalize(x_test, mean, std)

    device = resolve_device(torch, args.device)
    model = build_model(
        torch,
        rnn_type=args.rnn_type,
        input_size=x_train.shape[2],
        hidden_size=args.hidden_size,
        num_layers=args.num_layers,
        dropout=args.dropout,
        output_size=len(classes),
    ).to(device)

    train_dataset = torch.utils.data.TensorDataset(
        torch.tensor(x_train, dtype=torch.float32),
        torch.tensor(y_train, dtype=torch.long),
    )
    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=args.batch_size,
        shuffle=True,
    )

    criterion = torch.nn.CrossEntropyLoss(
        weight=class_weights(torch, y_train, len(classes), device)
        if args.class_weight == "balanced"
        else None
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    history: list[dict[str, float]] = []
    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        total_count = 0
        for batch_x, batch_y in train_loader:
            batch_x = batch_x.to(device)
            batch_y = batch_y.to(device)
            optimizer.zero_grad()
            logits = model(batch_x)
            loss = criterion(logits, batch_y)
            loss.backward()
            optimizer.step()
            total_loss += float(loss.item()) * int(batch_y.shape[0])
            total_count += int(batch_y.shape[0])

        train_loss = total_loss / max(total_count, 1)
        if epoch == 1 or epoch % 5 == 0 or epoch == args.epochs:
            print(f"epoch={epoch} train_loss={train_loss:.6f}")
        history.append({"epoch": epoch, "train_loss": round(train_loss, 6)})

    predictions = predict(torch, model, x_test, device=device, batch_size=args.batch_size)
    metrics = compute_metrics(y_test.tolist(), predictions, classes)
    metrics.update(
        {
            "rnn_type": args.rnn_type,
            "train_sequences": int(x_train.shape[0]),
            "test_sequences": int(x_test.shape[0]),
            "sequence_length": int(x_train.shape[1]),
            "feature_count": int(x_train.shape[2]),
            "class_counts_train": count_labels(y_train.tolist(), classes),
            "class_counts_test": count_labels(y_test.tolist(), classes),
            "history": history,
        }
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "input_size": int(x_train.shape[2]),
            "hidden_size": args.hidden_size,
            "num_layers": args.num_layers,
            "dropout": args.dropout,
            "output_size": len(classes),
            "classes": classes,
            "feature_columns": feature_columns,
            "rnn_type": args.rnn_type,
        },
        args.output_dir / "model.pt",
    )
    write_json(
        args.output_dir / "metadata.json",
        {
            "classes": classes,
            "feature_columns": feature_columns,
            "rnn_type": args.rnn_type,
            "hidden_size": args.hidden_size,
            "num_layers": args.num_layers,
            "dropout": args.dropout,
        },
    )
    write_json(
        args.output_dir / "normalization.json",
        {"mean": mean.reshape(-1).tolist(), "std": std.reshape(-1).tolist()},
    )
    write_json(args.output_dir / "metrics.json", metrics)
    print(f"wrote model artifacts to {args.output_dir}")
    print(json.dumps(metrics, indent=2, sort_keys=True))


def load_npz(np: Any, path: Path) -> dict[str, Any]:
    with np.load(path, allow_pickle=False) as data:
        return {key: data[key] for key in data.files}


def resolve_device(torch: Any, requested: str) -> Any:
    if requested == "cpu":
        return torch.device("cpu")
    if requested == "cuda":
        if not torch.cuda.is_available():
            raise SystemExit("CUDA was requested but is not available")
        return torch.device("cuda")
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def build_model(
    torch: Any,
    *,
    rnn_type: str,
    input_size: int,
    hidden_size: int,
    num_layers: int,
    dropout: float,
    output_size: int,
) -> Any:
    class RNNClassifier(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            rnn_cls = torch.nn.GRU if rnn_type == "gru" else torch.nn.LSTM
            self.rnn = rnn_cls(
                input_size=input_size,
                hidden_size=hidden_size,
                num_layers=num_layers,
                batch_first=True,
                dropout=dropout if num_layers > 1 else 0.0,
            )
            self.classifier = torch.nn.Linear(hidden_size, output_size)

        def forward(self, values: Any) -> Any:
            _, hidden = self.rnn(values)
            if isinstance(hidden, tuple):
                hidden = hidden[0]
            return self.classifier(hidden[-1])

    return RNNClassifier()


def feature_stats(np: Any, values: Any) -> tuple[Any, Any]:
    mean = values.mean(axis=(0, 1), keepdims=True)
    std = values.std(axis=(0, 1), keepdims=True)
    std = np.where(std < 1e-6, 1.0, std)
    return mean, std


def normalize(values: Any, mean: Any, std: Any) -> Any:
    return (values - mean) / std


def class_weights(torch: Any, labels: Any, class_count: int, device: Any) -> Any:
    counts = Counter(int(label) for label in labels)
    total = sum(counts.values())
    weights = [
        total / max(class_count * counts.get(index, 1), 1)
        for index in range(class_count)
    ]
    return torch.tensor(weights, dtype=torch.float32, device=device)


def predict(torch: Any, model: Any, values: Any, *, device: Any, batch_size: int) -> list[int]:
    dataset = torch.utils.data.TensorDataset(torch.tensor(values, dtype=torch.float32))
    loader = torch.utils.data.DataLoader(dataset, batch_size=batch_size)
    predictions: list[int] = []
    model.eval()
    with torch.no_grad():
        for (batch_x,) in loader:
            logits = model(batch_x.to(device))
            predictions.extend(logits.argmax(dim=1).cpu().tolist())
    return [int(value) for value in predictions]


def compute_metrics(y_true: list[int], y_pred: list[int], classes: list[str]) -> dict[str, Any]:
    class_count = len(classes)
    confusion = [[0 for _ in range(class_count)] for _ in range(class_count)]
    for actual, predicted in zip(y_true, y_pred):
        confusion[actual][predicted] += 1

    total = len(y_true)
    correct = sum(confusion[index][index] for index in range(class_count))
    per_class: dict[str, dict[str, float]] = {}
    f1_values: list[float] = []
    for index, label in enumerate(classes):
        tp = confusion[index][index]
        fp = sum(confusion[row][index] for row in range(class_count) if row != index)
        fn = sum(confusion[index][column] for column in range(class_count) if column != index)
        precision = tp / max(tp + fp, 1)
        recall = tp / max(tp + fn, 1)
        f1 = (2 * precision * recall) / max(precision + recall, 1e-12)
        f1_values.append(f1)
        per_class[label] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "support": sum(confusion[index]),
        }

    normal_index = classes.index("normal") if "normal" in classes else None
    normal_false_positive_rate = 0.0
    scan_recall = 0.0
    if normal_index is not None:
        normal_total = sum(confusion[normal_index])
        normal_missed = normal_total - confusion[normal_index][normal_index]
        normal_false_positive_rate = normal_missed / max(normal_total, 1)
        scan_total = sum(
            confusion[row][column]
            for row in range(class_count)
            for column in range(class_count)
            if row != normal_index
        )
        scan_correct = sum(
            confusion[row][column]
            for row in range(class_count)
            for column in range(class_count)
            if row != normal_index and column != normal_index
        )
        scan_recall = scan_correct / max(scan_total, 1)

    return {
        "accuracy": round(correct / max(total, 1), 6),
        "macro_f1": round(sum(f1_values) / max(len(f1_values), 1), 6),
        "normal_false_positive_rate": round(normal_false_positive_rate, 6),
        "scan_recall": round(scan_recall, 6),
        "per_class": per_class,
        "confusion_matrix": confusion,
        "classes": classes,
    }


def count_labels(labels: list[int], classes: list[str]) -> dict[str, int]:
    counts = Counter(labels)
    return {label: counts.get(index, 0) for index, label in enumerate(classes)}


def write_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(payload, file, indent=2, sort_keys=True)
        file.write("\n")


if __name__ == "__main__":
    main()
