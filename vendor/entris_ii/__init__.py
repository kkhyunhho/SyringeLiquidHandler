"""Sartorius Entris-II SBI controller package.

Exports the single-class facade ``PrecisionScaleController`` and the
``WeightReading`` value type, modelled on coport-uni/SyringePumpController.
"""

from .precision_scale_controller import (
    PrecisionScaleController,
    WeightReading,
)

__all__ = ["PrecisionScaleController", "WeightReading"]
