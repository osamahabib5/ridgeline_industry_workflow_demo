"""Compatibility entry point for the Ridgeline RFQ evaluator."""

if __package__:
    from .ris_evaluation import main
else:
    from ris_evaluation import main


if __name__ == "__main__":
    raise SystemExit(main())
