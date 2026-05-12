# Zoom to Parent Gate Feature

## Overview

When you select a parent gate for sub-gating, the plot now automatically **zooms in to show only the points within that parent gate**. This makes it much easier to create precise child gates within a parent population.

---

## How It Works

### **Before** (Old Behavior)
- Select parent gate → all data still visible
- Hard to see details within parent population
- Child gate creation requires zooming manually

### **After** (New Behavior)
- Select parent gate → **view automatically zooms to parent points**
- Only points within parent are displayed
- Child gate creation is much easier
- Select "None" → view returns to all data

---

## Visual Example

```
BEFORE (All data visible):
┌─────────────────────────┐
│ · · · · · · · · · · · · │
│ · · ┌─────────┐ · · · · │
│ · · │ Parent  │ · · · · │  ← All 100,000 points shown
│ · · │  R1     │ · · · · │     Hard to see detail inside R1
│ · · └─────────┘ · · · · │
│ · · · · · · · · · · · · │
└─────────────────────────┘

AFTER (Zoomed to parent):
┌─────────────────────────┐
│                         │
│    ┌─────────────┐      │
│    │   Parent R1 │      │  ← Only 10,000 points from R1
│    │             │      │     Clear view for sub-gating
│    │  • • • •    │      │     Easy to create child gates
│    │ • • • • •   │      │
│    └─────────────┘      │
│                         │
└─────────────────────────┘
```

---

## Usage

### Step 1: Create a Parent Gate
```
1. Select tool (Rectangle, Polygon, or Ellipse)
2. Draw a gate around your population of interest
3. Gate created: R1 (10,000 events)
```

### Step 2: Select Parent for Sub-gating
```
1. Go to "Parent Gate" selector (right panel)
2. Click on "R1"
3. Message panel shows:
   "Sub-gating: Children of 'R1'"
   "Displaying 10,000 points from 'R1'"
```

### Step 3: Plot Automatically Zooms
```
✓ View zooms to show only R1 points
✓ Parent gate outline shown for reference
✓ Other gates hidden (not relevant in this view)
✓ Axis scales adjusted to fit R1 data
```

### Step 4: Create Child Gate
```
1. Select tool (Polygon, Rectangle, or Ellipse)
2. Draw gate within the zoomed view
3. Gate P1 created as child of R1
4. Percentage shown relative to R1 (not total)
```

### Step 5: Return to Full View
```
1. Select "None" in Parent Gate selector
2. Message panel shows:
   "Sub-gating: Root-level gates (no parent)"
   "View: Showing all data"
3. Plot returns to full dataset view
```

---

## Message Panel Feedback

### When Selecting Parent:
```
[Message Log]
Sub-gating: Children of 'R1'
Displaying 10,000 points from 'R1'
```

### When Creating Child Gate:
```
[Message Log]
Gate 'P1' (child of R1): 2,000 events (20.0%)
```
_(Percentage is 20% of R1's 10,000 points)_

### When Returning to Full View:
```
[Message Log]
Sub-gating: Root-level gates (no parent)
View: Showing all data
```

---

## Benefits

### 1. **Better Visibility**
- See fine details within parent population
- Easier to identify sub-populations
- No clutter from irrelevant data points

### 2. **Easier Gate Creation**
- Click-drag distances are more appropriate
- Polygon vertices easier to place precisely
- Natural workflow for hierarchical gating

### 3. **Standard Flow Cytometry Workflow**
- Matches professional software (FlowJo, etc.)
- Intuitive for experienced users
- Teaches best practices for beginners

### 4. **Context Awareness**
- Only relevant gates shown (parent outline)
- Clear visual focus on current population
- No confusion about what you're gating

---

## Technical Details

### What Happens Internally

1. **Parent Selection**: `_on_parent_change()` called
   - Stores parent gate UID
   - Calls `_refresh_plot(log_zoom=True)`

2. **Plot Refresh**: `_refresh_plot()` checks for parent
   - If parent selected AND matches current channels:
     - Apply parent gate mask to data
     - Filter x, y arrays to parent points only
     - Plot only filtered data
     - Show only parent gate outline
   - If no parent:
     - Plot all data
     - Show all gates

3. **Axis Scaling**: matplotlib auto-scales
   - Axes automatically fit to filtered data
   - Creates natural "zoom" effect
   - No manual axis limit adjustment needed

### Channel Compatibility

**Important**: Zoom only works when parent gate channels match current plot:
- Parent gate: FSC-A vs SSC-A
- Current plot: FSC-A vs SSC-A → ✓ Zoom works
- Current plot: CD3 vs CD4 → ✗ Zoom disabled, show all data

**Why?** Can't filter on different channels than the gate was defined on.

---

## Edge Cases Handled

### 1. **Channel Mismatch**
```
Parent gate R1: FSC-A vs SSC-A
Current plot: CD3 vs CD4
Result: Show all data (can't apply R1 to different channels)
```

### 2. **No Parent Selected**
```
Parent selector: "None"
Result: Show all data, all gates visible
```

### 3. **Parent Gate Deleted**
```
Parent R1 selected, then R1 deleted
Result: _selected_parent_uid still set but gate not found
Code handles: parent_gate is None, shows all data
```

### 4. **Empty Parent (No Points)**
```
Parent gate with 0 events (edge case)
Result: Empty plot, parent outline shown
Message: "Displaying 0 points from 'R1'"
```

---

## Workflow Example

### Complete Sub-gating Workflow

```
Goal: Gate CD4+ T cells within lymphocytes

Step 1: Gate lymphocytes
  - Plot: FSC-A vs SSC-A
  - Create: Rectangle R1 (lymphocyte cloud)
  - Result: 50,000 events

Step 2: Focus on lymphocytes
  - Select: R1 in Parent Gate selector
  - View: Zooms to 50,000 lymphocyte points
  - Message: "Displaying 50,000 points from 'R1'"

Step 3: Gate CD4+ cells
  - Switch channels: CD3 vs CD4
  - View: Returns to all data (different channels!)
  - Message: "View: Showing all data"

  Wait, we need a different approach...

Step 3 (Revised): Gate on forward/side scatter first
  - Stay on: FSC-A vs SSC-A (same as R1)
  - Create: Polygon P1 within R1 (tighter lymphocyte definition)
  - Result: 45,000 events (90% of R1)

Step 4: Now switch to fluorescence
  - Channels: CD3 vs CD4
  - Parent: Select P1
  - View: Shows all data (different channels)
  - Create new gate on these channels

Alternative: Create separate parent gates for each channel combination
```

---

## Tips

### Best Practices

1. **Create parent on same channels as children**
   - Keep FSC/SSC gates on FSC/SSC
   - Keep fluorescence gates on fluorescence

2. **Use "None" to see context**
   - Periodically select "None" to see full picture
   - Verify parent gate placement is correct
   - Check for populations you might have missed

3. **Multiple levels of hierarchy**
   - R1: All cells → P1: Lymphocytes → P2: CD4+ → E1: Memory
   - Each level zooms to parent
   - Build up complex gating strategies

4. **Return to navigate mode for panning**
   - When zoomed, you might want to pan around
   - Switch to Navigate mode
   - Pan/zoom manually if needed

---

## Comparison with Professional Software

### FlowJo
- ✓ Zooms to parent when double-clicking gate
- ✓ Shows only parent points
- FlowCyt: Automatic zoom on parent selection

### FCS Express
- ✓ "Focus on gate" feature
- ✓ Filters display to gated population
- FlowCyt: Same behavior, automatic

### FlowCore (R/Bioconductor)
- Manual subsetting required
- No automatic zoom
- FlowCyt: More user-friendly

---

## Keyboard Shortcuts (Future Enhancement)

Potential additions:
- `Z`: Zoom to selected parent
- `A`: Show all data (reset zoom)
- `Esc`: Clear parent selection

---

## Summary

✅ **Automatic zoom** when parent selected
✅ **Clear view** of parent population only
✅ **Easy sub-gating** with focused view
✅ **Standard workflow** matching professional tools
✅ **Message feedback** confirming zoom state
✅ **Seamless switching** between zoomed and full view

This feature makes hierarchical gating intuitive and efficient!
