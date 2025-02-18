# BSD 3-Clause License; see https://github.com/scikit-hep/awkward-1.0/blob/main/LICENSE
__all__ = ("broadcast_fields",)
import awkward as ak
from awkward._backends.dispatch import backend_of
from awkward._backends.numpy import NumpyBackend
from awkward._behavior import behavior_of
from awkward._layout import wrap_layout

cpu = NumpyBackend.instance()


def broadcast_fields(
    *arrays,
    highlevel=True,
    behavior=None,
):
    """
    Args:
        arrays: Array-like data (anything #ak.to_layout recognizes).
        highlevel (bool): If True, return an #ak.Array; otherwise, return
            a low-level #ak.contents.Content subclass.
        behavior (None or dict): Custom #ak.behavior for the output array, if
            high-level.

    Return a list of arrays whose types contain the same number of fields. Unlike
    #ak.broadcast_arrays, this function does not require record types to occur at the
    same depths. Where fields are missing from one record, they are inserted at the same
    position with an `option[unknown]` type. This type is easily erased by ufunc and
    concatenation operations.

        >>> x, y = ak.broadcast_fields(
        ...     [{"x": {"y": 1, "z": 2, "w": [1]}}],
        ...     [{"x": [{"y": 1}]}],
        ... )
        >>> x.type.show()
        1 * {
            x: {
                y: int64,
                z: int64,
                w: var * int64
            }
        }
        >>> y.type.show()
        1 * {
            x: var * {
                y: int64,
                z: ?unknown,
                w: ?unknown
            }
        }

    """
    with ak._errors.OperationErrorContext(
        "ak.broadcast_fields",
        {
            "arrays": arrays,
            "highlevel": highlevel,
            "behavior": behavior,
        },
    ):
        return _impl(arrays, highlevel, behavior)


def _impl(arrays, highlevel, behavior):
    backend = backend_of(*arrays, default=cpu)
    layouts = [ak.to_layout(x).to_backend(backend) for x in arrays]
    behavior = behavior_of(*arrays, behavior=behavior)

    def identity(content):
        return content

    def descend_to_record_or_identity(layout, pullback=identity):
        assert layout is not None
        if layout.is_record or layout.is_identity_like:
            return pullback, layout
        elif layout.is_option or layout.is_indexed or layout.is_list:

            def next_pull(content):
                return pullback(layout.copy(content=content))

            return descend_to_record_or_identity(layout.content, next_pull)
        elif layout.is_leaf:
            return pullback, layout
        elif layout.is_union:
            raise TypeError("unions are not supported")
        else:
            raise AssertionError("unexpected content type")

    # Like broadcast_and_apply, we want to walk into each layout, correct the structure, and then rebuilt the arrays
    # We do this using "pull back" functions that accept a child content, and return the top-level layout. Unlike
    # layout.copy, the pull-back functions can be arbitrarily deep: the closures maintain the structure of the array
    def recurse(inputs):
        # Descend to records, identities, or leaves
        pullbacks, next_inputs = zip(
            *[descend_to_record_or_identity(x) for x in inputs]
        )
        # We can only work with all non-record, or all record/identity
        if any(c.is_record for c in next_inputs):
            if not all(c.is_record or c.is_identity_like for c in next_inputs):
                raise AssertionError(
                    "if any inputs are records, all inputs must be records or identities"
                )
        # With no records, we can exit here
        else:
            return [pull(layout) for pull, layout in zip(pullbacks, next_inputs)]

        # Broadcast the fields of only the records
        records = [r for r in next_inputs if r.is_record]
        all_fields = ak._util.unique_list(
            [f for layout in records for f in layout.fields]
        )

        # Build a list of layouts for each field, i.e. [{x: aaaa, y: aaaa}, {x: bbbb, y: bbbb}] becomes
        # [[aaaa, bbbb], [aaaa, bbbb]], where fields = [x, y]
        # These layouts will be "broadcast" against each other, hence the per-field ordering
        layouts_by_field = []
        for field in all_fields:
            layouts_to_recurse = []
            layouts_for_field = []
            for layout in records:
                if layout.has_field(field):
                    layouts_for_field.append(None)
                    layouts_to_recurse.append(layout.content(field))
                else:
                    layouts_for_field.append(layout.maybe_content(field))

            # We only want to recurse into non-missing fields, so we build this list separately as a generator
            recursed_field_layouts = iter(recurse(layouts_to_recurse))

            # Now we build the final list of layouts for this field, choosing between the recursion result and the
            # original layout according to whether the layout was recursed into
            # The pattern here is that `layouts_for_field` maintains positional correspondence with the `records`,
            # but uses `None` as a token for "recursed". In this case, we take the layout from `recursed_field_layouts`
            # using the knowledge that `len(layouts_to_recurse)` corresponds to the number of `None`s
            layouts_by_field.append(
                [
                    next(recursed_field_layouts) if layout is None else layout
                    for layout in layouts_for_field
                ]
            )

        # Now we transpose the list-of-lists to group layouts by original record, instead of by the field
        layouts_by_record = list(zip(*layouts_by_field))
        # Rebuild the original records with the new fields
        next_records = iter(
            [
                record.copy(
                    fields=all_fields,
                    contents=contents,
                )
                for record, contents in zip(records, layouts_by_record)
            ]
        )

        # Merge the records and identities
        inner_layouts = [
            (layout if layout.is_identity_like else next(next_records))
            for layout in next_inputs
        ]

        # Rebuild the outermost layouts using pull-back functions
        return [pull(layout) for pull, layout in zip(pullbacks, inner_layouts)]

    return [
        wrap_layout(x, highlevel=highlevel, behavior=behavior) for x in recurse(layouts)
    ]
