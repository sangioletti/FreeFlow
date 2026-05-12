# Fix: Parent Gate Selection Persists for Sub-gating

## Problem

**User Report**: "When using the buttons to choose a parent gate, the code correctly zooms to the selected region, but then the parent gate goes back to being None. As a result, I can never create child gates."

**Root Cause**: The `_refresh_parent_selector()` function was resetting `_selected_parent_uid = None` every time it rebuilt the radio buttons. This happened after every plot refresh, which meant:

1. User selects a parent gate (e.g., "P1")
2. System zooms to show only P1's points ✓
3. Plot refreshes and rebuilds radio buttons
4. `_selected_parent_uid` gets reset to `None` ✗
5. When user creates a new gate, it has no parent ✗

---

## Solution

**Fixed**: The parent gate selection now persists across plot refreshes!

### Code Changes

**File**: `flowcyt/app.py`, line ~354-361

**Before** (broken):
```python
# Build list: "None" + gate names
options = ["None"] + [g.name for g in self.gate_mgr.gates]
self._radio_parent = RadioButtons(self.ax_parent, options, active=0)  # Always "None"
self._radio_parent.on_clicked(self._on_parent_change)
self._selected_parent_uid = None  # ❌ This resets the selection!
```

**After** (fixed):
```python
# Build list: "None" + gate names
options = ["None"] + [g.name for g in self.gate_mgr.gates]

# Find which option should be active based on current selection
active_idx = 0  # Default to "None"
if self._selected_parent_uid:
    for i, gate in enumerate(self.gate_mgr.gates):
        if gate.uid == self._selected_parent_uid:
            active_idx = i + 1  # +1 because "None" is at index 0
            break

self._radio_parent = RadioButtons(self.ax_parent, options, active=active_idx)
self._radio_parent.on_clicked(self._on_parent_change)
# ✓ Don't reset _selected_parent_uid - preserve current selection!
```

### What Changed

1. **Preserves selection**: The radio button now shows the currently selected parent gate
2. **Correct active button**: The radio button for the selected parent is visually highlighted
3. **No more reset**: The `_selected_parent_uid` is NOT reset to None after refreshing

---

## How to Use Sub-gating Now

### Step 1: Select Parent Gate
1. Create a parent gate (e.g., P1, R1, or E1)
2. In the "Parent Gate" panel (right side), click the parent gate's name
3. **View zooms** to show only points inside the parent gate
4. **Radio button stays selected** showing which parent is active

### Step 2: Create Child Gates
1. While parent is selected, create new gates (polygon, rectangle, or ellipse)
2. These gates will be children of the selected parent
3. Child gate statistics show percentage **relative to parent**, not total
4. Message panel shows: "Gate 'E1' (child of P1): X events (Y%)"

### Step 3: Exit Sub-gating (Zoom Back Out)
1. Click "**None**" in the "Parent Gate" panel
2. View zooms back out to show all data points
3. New gates will be created as top-level gates (no parent)
4. Message panel shows: "Sub-gating: Root-level gates (no parent)"

---

## Visual Workflow Example

```
Initial State:
┌─────────────────┐
│ Parent Gate     │
│ ◉ None         │  ← Default (showing all data)
│ ○ P1           │
│ ○ R1           │
└─────────────────┘

After Selecting P1:
┌─────────────────┐
│ Parent Gate     │
│ ○ None         │
│ ◉ P1           │  ← Selected (view zoomed to P1)
│ ○ R1           │
└─────────────────┘
Plot shows: Only points inside P1
New gates: Will be children of P1

After Clicking "None":
┌─────────────────┐
│ Parent Gate     │
│ ◉ None         │  ← Back to default (view zoomed out)
│ ○ P1           │
│ ○ R1           │
└─────────────────┘
Plot shows: All data points
New gates: Will be top-level (no parent)
```

---

## Testing Sub-gating

### Test 1: Parent Selection Persists
1. Load an FCS file
2. Create gate P1 (any type)
3. Select P1 from "Parent Gate" radio buttons
4. **Check**: Radio button shows P1 selected (●)
5. **Check**: View zooms to show only P1 points
6. **Check**: Message panel says "Children of 'P1'"
7. Create gate E1 (ellipse)
8. **Check**: Message shows "Gate 'E1' (child of P1)"
9. **Check**: P1 is STILL selected in radio buttons ✓

### Test 2: Child Gate Percentages
1. Create parent P1 with ~10,000 events
2. Select P1 as parent
3. Create child E1 with ~2,000 events
4. **Check**: E1 shows "2,000 events (20.0%)"
   - This is 2,000/10,000 = 20% (relative to P1) ✓
   - NOT 2,000/50,000 = 4% (relative to total) ✗

### Test 3: Exit Sub-gating
1. With parent P1 selected
2. Click "None" in Parent Gate panel
3. **Check**: View zooms back out to show all data
4. **Check**: Message shows "Root-level gates (no parent)"
5. Create gate R1
6. **Check**: R1 is top-level gate (no parent)

---

## Expected Behavior

### ✅ What Should Happen Now

- **Parent stays selected**: Radio button shows correct parent after plot refreshes
- **Zoom persists**: View stays zoomed to parent until "None" is clicked
- **Child gates work**: New gates are children of selected parent
- **Percentages correct**: Child percentages relative to parent, not total
- **Easy exit**: Click "None" to zoom out and return to root-level gating

### ❌ What Was Broken Before

- Parent selection reset to "None" after every refresh
- Radio button always showed "None" even when parent was selected
- Impossible to create child gates (parent always null)
- View zoomed to parent but then immediately reset

---

## Debug Output

When creating gates, watch the terminal for:

```
[DEBUG] Created gate 'E1' with parent_gate_uid=abc123
[DEBUG]   _selected_parent_uid was: abc123
[DEBUG] Gate 'E1' is child of parent_uid=abc123
[DEBUG]   Parent count: 10,000, Child count: 2,000
[DEBUG]   Percentage: 2,000 / 10,000 = 20.0%
```

If you see `parent_gate_uid=None` when you expected a parent, that indicates a problem.

---

## Summary

✅ **Fixed**: Parent gate selection now persists across plot refreshes
✅ **Result**: Sub-gating works correctly - you can create child gates!
✅ **Exit**: Click "None" in Parent Gate panel to zoom back out
✅ **Visual feedback**: Radio button shows which parent is currently selected

The parent gate will remain selected until you explicitly click "None" or select a different parent!
