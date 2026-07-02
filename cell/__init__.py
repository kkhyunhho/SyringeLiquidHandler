"""The cell layer: device drivers composed behind the ``Cell`` protocol.

- ``cell_protocol`` — the ``Cell`` interface + ``CellError`` hierarchy.
- ``pump_gantry_cell`` — ``PumpGantryCell`` (cell1–3): pump + XZ gantry.
- ``balance_linear_cell`` — ``BalanceLinearCell`` (cell4): balance + linear rail.

Import the concrete classes from their submodules, e.g.
``from cell.pump_gantry_cell import PumpGantryCell``.
"""
