#! /usr/bin/env python3
# Read from stdin, spit out C header or body.

import argparse
import copy
import fileinput
import re

from collections import namedtuple

Enumtype = namedtuple('Enumtype', ['name', 'value'])

type2size = {
    'pad': 1,
    'struct channel_id': 32,
    'struct short_channel_id': 8,
    'struct ipv6': 16,
    'secp256k1_ecdsa_signature': 64,
    'struct preimage': 32,
    'struct pubkey': 33,
    'struct sha256': 32,
    'struct bitcoin_blkid': 32,
    'struct bitcoin_txid': 32,
    'struct secret': 32,
    'struct amount_msat': 8,
    'struct amount_sat': 8,
    'u64': 8,
    'u32': 4,
    'u16': 2,
    'u8': 1,
    'bool': 1
}

# These struct array helpers require a context to allocate from.
varlen_structs = [
    'peer_features',
    'gossip_getnodes_entry',
    'failed_htlc',
    'utxo',
    'bitcoin_tx',
    'wirestring',
]


class FieldType(object):
    def __init__(self, name):
        self.name = name

    def is_assignable(self):
        return self.name in ['u8', 'u16', 'u32', 'u64', 'bool', 'struct amount_msat', 'struct amount_sat'] or self.name.startswith('enum ')

    # We only accelerate the u8 case: it's common and trivial.
    def has_array_helper(self):
        return self.name in ['u8']

    def base(self):
        basetype = self.name
        if basetype.startswith('struct '):
            basetype = basetype[7:]
        elif basetype.startswith('enum '):
            basetype = basetype[5:]
        return basetype

    # Returns base size
    @staticmethod
    def _typesize(typename):
        if typename in type2size:
            return type2size[typename]
        elif typename.startswith('struct ') or typename.startswith('enum '):
            # We allow unknown structures/enums, for extensibility (can only happen
            # if explicitly specified in csv)
            return 0
        else:
            raise ValueError('Unknown typename {}'.format(typename))


# Full (message, fieldname)-mappings
typemap = {
    ('update_fail_htlc', 'reason'): FieldType('u8'),
    ('node_announcement', 'alias'): FieldType('u8'),
    ('update_add_htlc', 'onion_routing_packet'): FieldType('u8'),
    ('update_fulfill_htlc', 'payment_preimage'): FieldType('struct preimage'),
    ('error', 'data'): FieldType('u8'),
    ('shutdown', 'scriptpubkey'): FieldType('u8'),
    ('node_announcement', 'rgb_color'): FieldType('u8'),
    ('node_announcement', 'addresses'): FieldType('u8'),
    ('node_announcement', 'ipv6'): FieldType('struct ipv6'),
    ('announcement_signatures', 'short_channel_id'): FieldType('struct short_channel_id'),
    ('channel_announcement', 'short_channel_id'): FieldType('struct short_channel_id'),
    ('channel_update', 'short_channel_id'): FieldType('struct short_channel_id'),
    ('revoke_and_ack', 'per_commitment_secret'): FieldType('struct secret'),
    ('channel_reestablish_option_data_loss_protect', 'your_last_per_commitment_secret'): FieldType('struct secret'),
    ('channel_update', 'fee_base_msat'): FieldType('u32'),
    ('final_incorrect_htlc_amount', 'incoming_htlc_amt'): FieldType('struct amount_msat'),
}

# Partial names that map to a datatype
partialtypemap = {
    'signature': FieldType('secp256k1_ecdsa_signature'),
    'features': FieldType('u8'),
    'channel_id': FieldType('struct channel_id'),
    'chain_hash': FieldType('struct bitcoin_blkid'),
    'funding_txid': FieldType('struct bitcoin_txid'),
    'pad': FieldType('pad'),
    'msat': FieldType('struct amount_msat'),
    'satoshis': FieldType('struct amount_sat'),
}

# Size to typename match
sizetypemap = {
    33: FieldType('struct pubkey'),
    32: FieldType('struct sha256'),
    8: FieldType('u64'),
    4: FieldType('u32'),
    2: FieldType('u16'),
    1: FieldType('u8')
}


# It would be nicer if we had put '*u8' in spec and disallowed bare lenvar.
# In practice we only recognize lenvar when it's the previous field.

# size := baresize | arraysize
# baresize := simplesize | lenvar
# simplesize := number | type
# arraysize := length '*' type
# length := lenvar | number
class Field(object):
    def __init__(self, message, name, size, comments, prevname):
        self.message = message
        self.comments = comments
        self.name = name
        self.is_len_var = False
        self.lenvar = None
        self.num_elems = 1
        self.optional = False
        self.is_tlv = False

        # field name appended with '+' means this field contains a tlv
        if name.endswith('+'):
            self.is_tlv = True
            self.name = name[:-1]
            if self.name not in tlv_fields:
                # FIXME: use the rest of this
                tlv_includes, tlv_messages, tlv_comments = parse_tlv_file(self.name)
                tlv_fields[self.name] = tlv_messages

        # ? means optional field (not supported for arrays)
        if size.startswith('?'):
            self.optional = True
            size = size[1:]
        # If it's an arraysize, swallow prefix.
        elif '*' in size:
            number = size.split('*')[0]
            if number == prevname:
                self.lenvar = number
            else:
                self.num_elems = int(number)
            size = size.split('*')[1]
        elif options.bolt and size == prevname:
            # Raw length field, implies u8.
            self.lenvar = size
            size = '1'

        # Bolts use just a number: Guess type based on size.
        if options.bolt:
            base_size = int(size)
            self.fieldtype = Field._guess_type(message, self.name, base_size)
            # There are some arrays which we have to guess, based on sizes.
            tsize = FieldType._typesize(self.fieldtype.name)
            if base_size % tsize != 0:
                raise ValueError('Invalid size {} for {}.{} not a multiple of {}'
                                 .format(base_size,
                                         self.message,
                                         self.name,
                                         tsize))
            self.num_elems = int(base_size / tsize)
        else:
            # Real typename.
            self.fieldtype = FieldType(size)

    def is_padding(self):
        return self.name.startswith('pad')

    # Padding is always treated as an array.
    def is_array(self):
        return self.num_elems > 1 or self.is_padding()

    def is_variable_size(self):
        return self.lenvar is not None

    def needs_ptr_to_ptr(self):
        return self.is_variable_size() or self.optional

    def is_assignable(self):
        if self.is_array() or self.needs_ptr_to_ptr():
            return False
        return self.fieldtype.is_assignable()

    def has_array_helper(self):
        return self.fieldtype.has_array_helper()

    # Returns FieldType
    @staticmethod
    def _guess_type(message, fieldname, base_size):
        # Check for full (message, fieldname)-matches
        if (message, fieldname) in typemap:
            return typemap[(message, fieldname)]

        # Check for partial field names
        for k, v in partialtypemap.items():
            if k in fieldname:
                return v

        # Check for size matches
        if base_size in sizetypemap:
            return sizetypemap[base_size]

        raise ValueError('Unknown size {} for {}'.format(base_size, fieldname))


fromwire_impl_templ = """bool fromwire_{name}({ctx}const void *p{args})
{{
{fields}
\tconst u8 *cursor = p;
\tsize_t plen = tal_count(p);

\tif (fromwire_u16(&cursor, &plen) != {enum.name})
\t\treturn false;
{subcalls}
\treturn cursor != NULL;
}}
"""

fromwire_tlv_impl_templ = """static bool _fromwire_{tlv_name}_{name}({ctx}{args})
{{

\tsize_t start_len, plen;
{fields}
\tconst u8 *cursor = p;
\tplen = tal_count(p);
\tif (plen < len)
\t\treturn false;
\tstart_len = plen;
{subcalls}
\treturn cursor != NULL && (start_len - plen == len);
}}
"""

fromwire_header_templ = """bool fromwire_{name}({ctx}const void *p{args});
"""

towire_header_templ = """u8 *towire_{name}(const tal_t *ctx{args});
"""
towire_impl_templ = """u8 *towire_{name}(const tal_t *ctx{args})
{{
{field_decls}
\tu8 *p = tal_arr(ctx, u8, 0);
\ttowire_u16(&p, {enumname});
{subcalls}

\treturn memcheck(p, tal_count(p));
}}
"""

towire_tlv_templ = """u8 *towire_{name}(const tal_t *ctx{args})
{{
{field_decls}
\tu8 *p = tal_arr(ctx, u8, 0);
\ttowire_u16(&p, {enumname});
\ttowire_u16(&p, {len});
{subcalls}

\treturn memcheck(p, tal_count(p));
}}
"""

fromwire_tlv_templ = """bool frowire_{name}({ctx}const void *p{args})
{{
{fields}
\tconst u8 *cursor = p;
\tsize_t plen = tal_count(p);

\tif (frmwire_u16(&cursor, &plen) != {enum.name})
\t\treturn false;
{subcalls}
\treturn cursor != NULL;
}}
"""

printwire_header_templ = """void printwire_{name}(const char *fieldname, const u8 *cursor);
"""
printwire_impl_templ = """void printwire_{name}(const char *fieldname, const u8 *cursor)
{{
\tsize_t plen = tal_count(cursor);

\tif (fromwire_u16(&cursor, &plen) != {enum.name}) {{
\t\tprintf("WRONG TYPE?!\\n");
\t\treturn;
\t}}

{subcalls}

\tif (plen != 0)
\t\tprintf("EXTRA: %s\\n", tal_hexstr(NULL, cursor, plen));
}}
"""


class CCode(object):
    """Simple class to create indented C code"""
    def __init__(self):
        self.indent = 1
        self.single_indent = False
        self.code = []

    def append(self, lines):
        for line in lines.split('\n'):
            # Let us to the indenting please!
            assert '\t' not in line

            # Special case: } by itself is pre-unindented.
            if line == '}':
                self.indent -= 1
                self.code.append("\t" * self.indent + line)
                continue

            self.code.append("\t" * self.indent + line)
            if self.single_indent:
                self.indent -= 1
                self.single_indent = False

            if line.endswith('{'):
                self.indent += 1
            elif line.endswith('}'):
                self.indent -= 1
            elif line.startswith('for') or line.startswith('if'):
                self.indent += 1
                self.single_indent = True

    def __str__(self):
        assert self.indent == 1
        assert not self.single_indent
        return '\n'.join(self.code)


class Message(object):
    def __init__(self, name, enum, comments, is_tlv):
        self.name = name
        self.enum = enum
        self.comments = comments
        self.fields = []
        self.has_variable_fields = False
        self.is_tlv = is_tlv

    def checkLenField(self, field):
        # Optional fields don't have a len.
        if field.optional:
            return
        for f in self.fields:
            if f.name == field.lenvar:
                if f.fieldtype.name != 'u16' and options.bolt:
                    raise ValueError('Field {} has non-u16 length variable {} (type {})'
                                     .format(field.name, field.lenvar, f.fieldtype.name))

                if f.is_array() or f.needs_ptr_to_ptr():
                    raise ValueError('Field {} has non-simple length variable {}'
                                     .format(field.name, field.lenvar))
                f.is_len_var = True
                f.lenvar_for = field
                return
        raise ValueError('Field {} unknown length variable {}'
                         .format(field.name, field.lenvar))

    def addField(self, field):
        # We assume field lengths are 16 bit, to avoid overflow issues and
        # massive allocations.
        if field.is_variable_size():
            self.checkLenField(field)
            self.has_variable_fields = True
        elif field.fieldtype.base() in varlen_structs or field.optional:
            self.has_variable_fields = True
        self.fields.append(field)

    def print_fromwire_array(self, ctx, subcalls, basetype, f, name, num_elems):
        if f.has_array_helper():
            subcalls.append('fromwire_{}_array(&cursor, &plen, {}, {});'
                            .format(basetype, name, num_elems))
        else:
            subcalls.append('for (size_t i = 0; i < {}; i++)'
                            .format(num_elems))
            if f.fieldtype.is_assignable():
                subcalls.append('({})[i] = fromwire_{}(&cursor, &plen);'
                                .format(name, basetype))
            elif basetype in varlen_structs:
                subcalls.append('({})[i] = fromwire_{}({}, &cursor, &plen);'
                                .format(name, basetype, ctx))
            else:
                subcalls.append('fromwire_{}(&cursor, &plen, {} + i);'
                                .format(basetype, name))

    def print_tlv_fromwire(self, tlv_name):
        """ prints fromwire function definition for a TLV message.
        these are significantly different in that they take in a struct
        to populate, instead of fields, as well as a length to read in
        """
        ctx_arg = 'const tal_t *ctx, ' if self.has_variable_fields else ''
        args = 'const void *p, const u16 len, struct _tlv_msg_{name} *{name}'.format(name=self.name)
        fields = ['\t{} {};\n'.format(f.fieldtype.name, f.name) for f in self.fields if f.is_len_var]
        subcalls = CCode()
        for f in self.fields:
            basetype = f.fieldtype.base()
            if f.is_tlv:
                raise TypeError('Nested TLVs arent allowed!!')
            elif f.optional:
                raise TypeError('Optional fields on TLV messages not currently supported')

            for c in f.comments:
                subcalls.append('/*{} */'.format(c))

            if f.is_padding():
                subcalls.append('fromwire_pad(&cursor, &plen, {});'
                                .format(f.num_elems))
            elif f.is_array():
                name = '*{}->{}'.format(self.name, f.name)
                self.print_fromwire_array('ctx', subcalls, basetype, f, name,
                                          f.num_elems)
            elif f.is_variable_size():
                subcalls.append("// 2nd case {name}".format(name=f.name))
                typename = f.fieldtype.name
                # If structs are varlen, need array of ptrs to them.
                if basetype in varlen_structs:
                    typename += ' *'
                subcalls.append('{}->{} = {} ? tal_arr(ctx, {}, {}) : NULL;'
                                .format(self.name, f.name, f.lenvar, typename, f.lenvar))

                name = '{}->{}'.format(self.name, f.name)
                # Allocate these off the array itself, if they need alloc.
                self.print_fromwire_array('*' + f.name, subcalls, basetype, f,
                                          name, f.lenvar)
            else:
                if f.is_assignable():
                    if f.is_len_var:
                        s = '{} = fromwire_{}(&cursor, &plen);'.format(f.name, basetype)
                    else:
                        s = '{}->{} = fromwire_{}(&cursor, &plen);'.format(
                            self.name, f.name, basetype)
                else:
                    s = 'fromwire_{}(&cursor, &plen, *{}->{});'.format(
                        basetype, self.name, f.name)
                subcalls.append(s)

        return fromwire_tlv_impl_templ.format(
            tlv_name=tlv_name,
            name=self.name,
            ctx=ctx_arg,
            args=''.join(args),
            fields=''.join(fields),
            subcalls=str(subcalls)
        )

    def print_fromwire(self, is_header, tlv_name):
        if self.is_tlv:
            if is_header:
                return ''
            return self.print_tlv_fromwire(tlv_name)

        ctx_arg = 'const tal_t *ctx, ' if self.has_variable_fields else ''

        args = []

        for f in self.fields:
            if f.is_len_var or f.is_padding():
                continue
            elif f.is_array():
                args.append(', {} {}[{}]'.format(f.fieldtype.name, f.name, f.num_elems))
            elif f.is_tlv:
                args.append(', struct _{} *{}'.format(f.name, f.name))
            else:
                ptrs = '*'
                # If we're handing a variable array, we need a ptr-to-ptr.
                if f.needs_ptr_to_ptr():
                    ptrs += '*'
                # If each type is a variable length, we need a ptr to that.
                if f.fieldtype.base() in varlen_structs:
                    ptrs += '*'

                args.append(', {} {}{}'.format(f.fieldtype.name, ptrs, f.name))

        template = fromwire_header_templ if is_header else fromwire_impl_templ
        fields = ['\t{} {};\n'.format(f.fieldtype.name, f.name) for f in self.fields if f.is_len_var]

        subcalls = CCode()
        for f in self.fields:
            basetype = f.fieldtype.base()

            for c in f.comments:
                subcalls.append('/*{} */'.format(c))

            if f.is_padding():
                subcalls.append('fromwire_pad(&cursor, &plen, {});'
                                .format(f.num_elems))
            elif f.is_array():
                self.print_fromwire_array('ctx', subcalls, basetype, f, f.name,
                                          f.num_elems)
            elif f.is_tlv:
                if not f.is_variable_size():
                    raise TypeError('TLV {} not variable size'.format(f.name))
                subcalls.append('if (!fromwire__{tlv_name}(ctx, &cursor, &{tlv_len}, {tlv_name}))'
                                .format(tlv_name=f.name, tlv_len=f.lenvar))
                subcalls.append('return false;')
            elif f.is_variable_size():
                subcalls.append("//2nd case {name}".format(name=f.name))
                typename = f.fieldtype.name
                # If structs are varlen, need array of ptrs to them.
                if basetype in varlen_structs:
                    typename += ' *'
                subcalls.append('*{} = {} ? tal_arr(ctx, {}, {}) : NULL;'
                                .format(f.name, f.lenvar, typename, f.lenvar))

                # Allocate these off the array itself, if they need alloc.
                self.print_fromwire_array('*' + f.name, subcalls, basetype, f,
                                          '*' + f.name, f.lenvar)
            else:
                if f.optional:
                    assignable = f.fieldtype.is_assignable()
                    deref = '*'
                else:
                    deref = ''
                    assignable = f.is_assignable()

                if assignable:
                    if f.is_len_var:
                        s = '{} = fromwire_{}(&cursor, &plen);'.format(f.name, basetype)
                    else:
                        s = '{}*{} = fromwire_{}(&cursor, &plen);'.format(deref, f.name, basetype)
                elif basetype in varlen_structs:
                    s = '{}*{} = fromwire_{}(ctx, &cursor, &plen);'.format(deref, f.name, basetype)
                else:
                    s = 'fromwire_{}(&cursor, &plen, {}{});'.format(basetype, deref, f.name)

                if f.optional:
                    subcalls.append("if (!fromwire_bool(&cursor, &plen))\n"
                                    "*{} = NULL;\n"
                                    "else {{\n"
                                    "*{} = tal(ctx, {});\n"
                                    "{}\n"
                                    "}}"
                                    .format(f.name, f.name, f.fieldtype.name,
                                            s))
                else:
                    subcalls.append(s)

        return template.format(
            name=self.name,
            ctx=ctx_arg,
            args=''.join(args),
            fields=''.join(fields),
            enum=self.enum,
            subcalls=str(subcalls)
        )

    def print_towire_array(self, subcalls, basetype, f, num_elems, is_tlv=False):
        p_ref = '' if is_tlv else '&'
        msg_name = self.name + '->' if is_tlv else ''
        if f.has_array_helper():
            subcalls.append('towire_{}_array({}p, {}{}, {});'
                            .format(basetype, p_ref, msg_name, f.name, num_elems))
        else:
            subcalls.append('for (size_t i = 0; i < {}; i++)'
                            .format(num_elems))
            if f.fieldtype.is_assignable() or basetype in varlen_structs:
                subcalls.append('towire_{}({}p, {}{}[i]);'
                                .format(basetype, p_ref, msg_name, f.name))
            else:
                subcalls.append('towire_{}({}p, {}{} + i);'
                                .format(basetype, p_ref, msg_name, f.name))

    def print_tlv_towire(self, tlv_name):
        """ prints towire function definition for a TLV message."""
        field_decls = []
        for f in self.fields:
            if f.is_tlv:
                raise TypeError("Nested TLVs aren't allowed!! {}->{}".format(tlv_name, f.name))
            elif f.optional:
                raise TypeError("Optional fields on TLV messages not currently supported. {}->{}".format(tlv_name, f.name))
            if f.is_len_var:
                field_decls.append('\t{0} {1} = tal_count(&{2}->{3});'.format(
                    f.fieldtype.name, f.name, self.name, f.lenvar_for.name
                ))

        subcalls = CCode()
        for f in self.fields:
            basetype = f.fieldtype.base()
            for c in f.comments:
                subcalls.append('/*{} */'.format(c))

            if f.is_padding():
                subcalls.append('towire_pad(p, {});'.format(f.num_elems))
            elif f.is_array():
                self.print_towire_array(subcalls, basetype, f, f.num_elems, is_tlv=True)
            elif f.is_variable_size():
                self.print_towire_array(subcalls, basetype, f, f.lenvar, is_tlv=True)
            elif f.is_len_var:
                subcalls.append('towire_{}(p, {});'.format(basetype, f.name))
            else:
                subcalls.append('towire_{}(p, {}->{});'.format(basetype, self.name, f.name))
        return tlv_message_towire_stub.format(
            tlv_name=tlv_name,
            name=self.name,
            field_decls='\n'.join(field_decls),
            subcalls=str(subcalls))

    def print_towire(self, is_header, tlv_name):
        if self.is_tlv:
            if is_header:
                return ''
            return self.print_tlv_towire(tlv_name)
        template = towire_header_templ if is_header else towire_impl_templ
        args = []
        for f in self.fields:
            if f.is_padding() or f.is_len_var:
                continue
            if f.is_array():
                args.append(', const {} {}[{}]'.format(f.fieldtype.name, f.name, f.num_elems))
            elif f.is_tlv:
                args.append(', const struct _{} *{}'.format(f.name, f.name))
            elif f.is_assignable():
                args.append(', {} {}'.format(f.fieldtype.name, f.name))
            elif f.is_variable_size() and f.fieldtype.base() in varlen_structs:
                args.append(', const {} **{}'.format(f.fieldtype.name, f.name))
            else:
                args.append(', const {} *{}'.format(f.fieldtype.name, f.name))

        field_decls = []
        for f in self.fields:
            if f.is_len_var:
                if f.lenvar_for.is_tlv:
                    field_decls.append('\t{0} {1} = sizeof({2});'.format(
                        f.fieldtype.name, f.name, f.lenvar_for.name
                    ))
                else:
                    field_decls.append('\t{0} {1} = tal_count({2});'.format(
                        f.fieldtype.name, f.name, f.lenvar_for.name
                    ))

        subcalls = CCode()
        for f in self.fields:
            basetype = f.fieldtype.base()

            for c in f.comments:
                subcalls.append('/*{} */'.format(c))

            if f.is_padding():
                subcalls.append('towire_pad(&p, {});'
                                .format(f.num_elems))
            elif f.is_array():
                self.print_towire_array(subcalls, basetype, f, f.num_elems)
            elif f.is_tlv:
                if not f.is_variable_size():
                    raise TypeError('TLV {} not variable size'.format(f.name))
                subcalls.append('towire__{tlv_name}(&p, {tlv_name});'.format(
                    tlv_name=f.name))
            elif f.is_variable_size():
                self.print_towire_array(subcalls, basetype, f, f.lenvar)
            else:
                if f.optional:
                    if f.fieldtype.is_assignable():
                        deref = '*'
                    else:
                        deref = ''
                    subcalls.append("if (!{})\n"
                                    "towire_bool(&p, false);\n"
                                    "else {{\n"
                                    "towire_bool(&p, true);\n"
                                    "towire_{}(&p, {}{});\n"
                                    "}}".format(f.name, basetype, deref, f.name))
                else:
                    subcalls.append('towire_{}(&p, {});'
                                    .format(basetype, f.name))

        return template.format(
            name=self.name,
            args=''.join(args),
            enumname=self.enum.name,
            field_decls='\n'.join(field_decls),
            subcalls=str(subcalls),
        )

    def add_truncate_check(self, subcalls):
        # Report if truncated, otherwise print.
        subcalls.append('if (!cursor) {\n'
                        'printf("**TRUNCATED**\\n");\n'
                        'return;\n'
                        '}')

    def print_printwire_array(self, subcalls, basetype, f, num_elems):
        if f.has_array_helper():
            subcalls.append('printwire_{}_array(tal_fmt(NULL, "%s.{}", fieldname), &cursor, &plen, {});'
                            .format(basetype, f.name, num_elems))
        else:
            subcalls.append('printf("[");')
            subcalls.append('for (size_t i = 0; i < {}; i++) {{'
                            .format(num_elems))
            subcalls.append('{} v;'.format(f.fieldtype.name))
            if f.fieldtype.is_assignable():
                subcalls.append('v = fromwire_{}(&cursor, plen);'
                                .format(f.fieldtype.name, basetype))
            else:
                # We don't handle this yet!
                assert(basetype not in varlen_structs)

                subcalls.append('fromwire_{}(&cursor, &plen, &v);'
                                .format(basetype))

            self.add_truncate_check(subcalls)

            subcalls.append('printwire_{}(tal_fmt(NULL, "%s.{}", fieldname), &v);'
                            .format(basetype, f.name))
            subcalls.append('}')
            subcalls.append('printf("]");')

    def print_printwire(self, is_header):
        template = printwire_header_templ if is_header else printwire_impl_templ
        fields = ['\t{} {};\n'.format(f.fieldtype.name, f.name) for f in self.fields if f.is_len_var]

        subcalls = CCode()
        for f in self.fields:
            basetype = f.fieldtype.base()

            for c in f.comments:
                subcalls.append('/*{} */'.format(c))

            if f.is_len_var:
                subcalls.append('{} {} = fromwire_{}(&cursor, &plen);'
                                .format(f.fieldtype.name, f.name, basetype))
                self.add_truncate_check(subcalls)
                continue

            subcalls.append('printf("{}=");'.format(f.name))
            if f.is_padding():
                subcalls.append('printwire_pad(tal_fmt(NULL, "%s.{}", fieldname), &cursor, &plen, {});'
                                .format(f.name, f.num_elems))
                self.add_truncate_check(subcalls)
            elif f.is_array():
                self.print_printwire_array(subcalls, basetype, f, f.num_elems)
                self.add_truncate_check(subcalls)
            elif f.is_variable_size():
                self.print_printwire_array(subcalls, basetype, f, f.lenvar)
                self.add_truncate_check(subcalls)
            else:
                if f.optional:
                    subcalls.append("if (fromwire_bool(&cursor, &plen)) {")

                if f.is_assignable():
                    subcalls.append('{} {} = fromwire_{}(&cursor, &plen);'
                                    .format(f.fieldtype.name, f.name, basetype))
                else:
                    # Don't handle these yet.
                    assert(basetype not in varlen_structs)
                    subcalls.append('{} {};'.
                                    format(f.fieldtype.name, f.name))
                    subcalls.append('fromwire_{}(&cursor, &plen, &{});'
                                    .format(basetype, f.name))

                self.add_truncate_check(subcalls)
                subcalls.append('printwire_{}(tal_fmt(NULL, "%s.{}", fieldname), &{});'
                                .format(basetype, f.name, f.name))
                if f.optional:
                    subcalls.append("} else {")
                    self.add_truncate_check(subcalls)
                    subcalls.append("}")

        return template.format(
            name=self.name,
            fields=''.join(fields),
            enum=self.enum,
            subcalls=str(subcalls)
        )

    def print_struct(self):
        """ returns a string representation of this message as
        a struct"""
        if not self.is_tlv:
            raise TypeError('{} is not a TLV-message').format(self.name)

        fmt_fields = CCode()
        for f in self.fields:
            if f.is_len_var or f.is_padding():
                # there is no ethical padding under TLVs
                continue
            elif f.is_variable_size():
                fmt_fields.append('{} *{};'.format(f.fieldtype.name, f.name))
            elif f.is_array():
                fmt_fields.append('{} {}[{}];'.format(f.fieldtype.name, f.name, f.num_elems))
            else:
                fmt_fields.append('{} {};'.format(f.fieldtype.name, f.name))

        return tlv_msg_struct_template.format(
            msg_name=self.name,
            fields=str(fmt_fields))


tlv_message_towire_stub = """static void _towire_{tlv_name}_{name}(u8 **p, struct _tlv_msg_{name} *{name}) {{
{field_decls}
{subcalls}
}}
"""


tlv_msg_struct_template = """
struct _tlv_msg_{msg_name} {{
{fields}
}};
"""

tlv_struct_template = """
struct _{tlv_name} {{
{msg_type_structs}
}};
"""

tlv__type_impl_towire_fields = """\tif ({tlv_name}->{name}) {{
\t\ttowire_u16(p, {enum});
\t\ttowire_u16(p, sizeof(*{tlv_name}->{name}));
\t\t_towire_{tlv_name}_{name}(p, {tlv_name}->{name});
\t}}
"""

tlv__type_impl_towire_template = """static void towire__{tlv_name}(u8 **p, const struct _{tlv_name} *{tlv_name}) {{
{fields}}}
"""

tlv__type_impl_fromwire_template = """static bool fromwire__{tlv_name}(const tal_t *ctx, const u8 **p, const u16 *len, struct _{tlv_name} *{tlv_name}) {{
\tu16 msg_type, msg_len;
\tconst u8 *cursor = *p;
\tsize_t plen = tal_count(p);
\tif (plen != *len)
\t\treturn false;

\twhile (cursor && plen) {{
\t\tmsg_type = fromwire_u16(&cursor, &plen);
\t\tmsg_len = fromwire_u16(&cursor, &plen);
\t\tif (plen < msg_len) {{
\t\t\tfromwire_fail(&cursor, &plen);
\t\t\tbreak;
\t\t}}
\t\tswitch((enum {tlv_name}_type)msg_type) {{
{cases}\t\tdefault:
\t\t\t// FIXME: print a warning / message?
\t\t\tcursor += msg_len;
\t\t\tplen -= msg_len;
\t\t}}
\t}}
\treturn cursor != NULL;
}}
"""

case_tmpl = """\t\tcase {tlv_msg_enum}:
\t\t\tif (!_fromwire_{tlv_name}_{tlv_msg_name}({ctx_arg}cursor, msg_len, {tlv_name}->{tlv_msg_name}))
\t\t\t\treturn false;
\t\t\tbreak;
"""


def build_tlv_fromwires(tlv_fields):
    fromwires = []
    for field_name, messages in tlv_fields.items():
        fromwires.append(print_tlv_fromwire(field_name, messages))
    return fromwires


def build_tlv_towires(tlv_fields):
    towires = []
    for field_name, messages in tlv_fields.items():
        towires.append(print_tlv_towire(field_name, messages))
    return towires


def print_tlv_towire(tlv_field_name, messages):
    fields = ""
    for m in messages:
        fields += tlv__type_impl_towire_fields.format(
            tlv_name=tlv_field_name,
            enum=m.enum.name,
            name=m.name)
    return tlv__type_impl_towire_template.format(
        tlv_name=tlv_field_name,
        fields=fields)


def print_tlv_fromwire(tlv_field_name, messages):
    cases = ""
    for m in messages:
        ctx_arg = 'ctx, ' if m.has_variable_fields else ''
        cases += case_tmpl.format(ctx_arg=ctx_arg,
                                  tlv_msg_enum=m.enum.name,
                                  tlv_name=tlv_field_name,
                                  tlv_msg_name=m.name)
    return tlv__type_impl_fromwire_template.format(
        tlv_name=tlv_field_name,
        cases=cases)


def build_tlv_type_struct(name, messages):
    inner_structs = CCode()
    for m in messages:
        inner_structs.append('struct _tlv_msg_{} *{};'.format(m.name, m.name))
    return tlv_struct_template.format(
        tlv_name=name,
        msg_type_structs=str(inner_structs))


def build_tlv_type_structs(tlv_fields):
    structs = ''
    for name, messages in tlv_fields.items():
        structs += build_tlv_type_struct(name, messages)
    return structs


def find_message(messages, name):
    for m in messages:
        if m.name == name:
            return m

    return None


def find_message_with_option(messages, optional_messages, name, option):
    fullname = name + "_" + option.replace('-', '_')

    base = find_message(messages, name)
    if not base:
        raise ValueError('Unknown message {}'.format(name))

    m = find_message(optional_messages, fullname)
    if not m:
        # Add a new option.
        m = copy.deepcopy(base)
        m.name = fullname
        optional_messages.append(m)
    return m


def get_directory_prefix():
    # FIXME: use prefix of filename
    return "wire/"


def get_tlv_filename(field_name):
    return 'gen_{}_csv'.format(field_name)


def parse_tlv_file(tlv_field_name):
    tlv_includes = []
    tlv_messages = []
    tlv_comments = []
    tlv_prevfield = None
    with open(get_directory_prefix() + get_tlv_filename(tlv_field_name)) as f:
        for line in f:
            # #include gets inserted into header
            if line.startswith('#include '):
                tlv_includes.append(line)
                continue

            by_comments = line.rstrip().split('#')

            # Emit a comment if they included one
            if by_comments[1:]:
                tlv_comments.append(' '.join(by_comments[1:]))

            parts = by_comments[0].split(',')
            if parts == ['']:
                continue

            if len(parts) == 2:
                # eg commit_sig,132
                tlv_msg = Message(parts[0], Enumtype("TLV_" + parts[0].upper(), parts[1]), tlv_comments, True)
                tlv_messages.append(tlv_msg)

                tlv_comments = []
                tlv_prevfield = None
            else:
                if len(parts) == 4:
                    # eg commit_sig,0,channel-id,8 OR
                    #    commit_sig,0,channel-id,u64
                    m = find_message(tlv_messages, parts[0])
                    if m is None:
                        raise ValueError('Unknown message {}'.format(parts[0]))
                elif len(parts) == 5:
                    # eg.
                    # channel_reestablish,48,your_last_per_commitment_secret,32,option209
                    m = find_message_with_option(tlv_messages, messages_with_option, parts[0], parts[4])
                else:
                    raise ValueError('Line {} malformed'.format(line.rstrip()))

                f = Field(m.name, parts[2], parts[3], tlv_comments, tlv_prevfield)
                m.addField(f)
                # If it used prevfield as lenvar, keep that for next
                # time (multiple fields can use the same lenvar).
                if not f.lenvar:
                    tlv_prevfield = parts[2]
                tlv_comments = []
        return tlv_includes, tlv_messages, tlv_comments


parser = argparse.ArgumentParser(description='Generate C from CSV')
parser.add_argument('--header', action='store_true', help="Create wire header")
parser.add_argument('--bolt', action='store_true', help="Generate wire-format for BOLT")
parser.add_argument('--printwire', action='store_true', help="Create print routines")
parser.add_argument('headerfilename', help='The filename of the header')
parser.add_argument('enumname', help='The name of the enum to produce')
parser.add_argument('files', nargs='*', help='Files to read in (or stdin)')
options = parser.parse_args()

# Maps message names to messages
messages = []
messages_with_option = []
comments = []
includes = []
tlv_fields = {}
prevfield = None

# Read csv lines.  Single comma is the message values, more is offset/len.
for line in fileinput.input(options.files):
    # #include gets inserted into header
    if line.startswith('#include '):
        includes.append(line)
        continue

    by_comments = line.rstrip().split('#')

    # Emit a comment if they included one
    if by_comments[1:]:
        comments.append(' '.join(by_comments[1:]))

    parts = by_comments[0].split(',')
    if parts == ['']:
        continue

    if len(parts) == 2:
        # eg commit_sig,132
        messages.append(Message(parts[0], Enumtype("WIRE_" + parts[0].upper(), parts[1]), comments, False))
        comments = []
        prevfield = None
    else:
        if len(parts) == 4:
            # eg commit_sig,0,channel-id,8 OR
            #    commit_sig,0,channel-id,u64
            m = find_message(messages, parts[0])
            if m is None:
                raise ValueError('Unknown message {}'.format(parts[0]))
        elif len(parts) == 5:
            # eg.
            # channel_reestablish,48,your_last_per_commitment_secret,32,option209
            m = find_message_with_option(messages, messages_with_option, parts[0], parts[4])
        else:
            raise ValueError('Line {} malformed'.format(line.rstrip()))

        f = Field(m.name, parts[2], parts[3], comments, prevfield)
        m.addField(f)
        # If it used prevfield as lenvar, keep that for next
        # time (multiple fields can use the same lenvar).
        if not f.lenvar:
            prevfield = parts[2]
        comments = []


def construct_hdr_enums(msgs):
    enums = ""
    for m in msgs:
        for c in m.comments:
            enums += '\t/*{} */\n'.format(c)
        enums += '\t{} = {},\n'.format(m.enum.name, m.enum.value)
    return enums


def construct_impl_enums(msgs):
    return '\n\t'.join(['case {enum.name}: return "{enum.name}";'.format(enum=m.enum) for m in msgs])


def enum_header(enums, enumname):
    return format_enums(enum_header_template, enums, enumname)


def enum_impl(enums, enumname):
    return format_enums(enum_impl_template, enums, enumname)


def format_enums(template, enums, enumname):
    return template.format(
        enums=enums,
        enumname=enumname)


def build_hdr_enums(toplevel_enumname, toplevel_messages, tlv_fields):
    enum_set = ""
    enum_set += enum_header(construct_hdr_enums(toplevel_messages), toplevel_enumname)
    for field_name, tlv_messages in tlv_fields.items():
        enum_set += "\n"
        enum_set += enum_header(construct_hdr_enums(tlv_messages), field_name + '_type')
    return enum_set


def build_impl_enums(toplevel_enumname, toplevel_messages, tlv_fields):
    enum_set = ""
    enum_set += enum_impl(construct_impl_enums(toplevel_messages), toplevel_enumname)
    for field_name, tlv_messages in tlv_fields.items():
        enum_set += "\n"
        enum_set += enum_impl(construct_impl_enums(tlv_messages), field_name + '_type')
    return enum_set


def build_tlv_structs(tlv_fields):
    structs = ""
    for field_name, tlv_messages in tlv_fields.items():
        for m in tlv_messages:
            structs += m.print_struct()
    return structs


enum_header_template = """enum {enumname} {{
{enums}
}};
const char *{enumname}_name(int e);
"""

enum_impl_template = """
const char *{enumname}_name(int e)
{{
\tstatic char invalidbuf[sizeof("INVALID ") + STR_MAX_CHARS(e)];

\tswitch ((enum {enumname})e) {{
\t{enums}
\t}}

\tsnprintf(invalidbuf, sizeof(invalidbuf), "INVALID %i", e);
\treturn invalidbuf;
}}
"""

header_template = """/* This file was generated by generate-wire.py */
/* Do not modify this file! Modify the _csv file it was generated from. */
#ifndef LIGHTNING_{idem}
#define LIGHTNING_{idem}
#include <ccan/tal/tal.h>
#include <wire/wire.h>
{includes}
{formatted_hdr_enums}{tlv_structs}
{func_decls}
#endif /* LIGHTNING_{idem} */
"""

impl_template = """/* This file was generated by generate-wire.py */
/* Do not modify this file! Modify the _csv file it was generated from. */
#include <{headerfilename}>
#include <ccan/mem/mem.h>
#include <ccan/tal/str/str.h>
#include <stdio.h>
{formatted_impl_enums}
{func_decls}
"""

print_header_template = """/* This file was generated by generate-wire.py */
/* Do not modify this file! Modify the _csv file it was generated from. */
#ifndef LIGHTNING_{idem}
#define LIGHTNING_{idem}
#include <ccan/tal/tal.h>
#include <devtools/print_wire.h>
{includes}

void print{enumname}_message(const u8 *msg);

{func_decls}
#endif /* LIGHTNING_{idem} */
"""

print_template = """/* This file was generated by generate-wire.py */
/* Do not modify this file! Modify the _csv file it was generated from. */
#include "{headerfilename}"
#include <ccan/mem/mem.h>
#include <ccan/tal/str/str.h>
#include <common/utils.h>
#include <stdio.h>

void print{enumname}_message(const u8 *msg)
{{
\tswitch ((enum {enumname})fromwire_peektype(msg)) {{
\t{printcases}
\t}}

\tprintf("UNKNOWN: %s\\n", tal_hex(msg, msg));
}}

{func_decls}
"""

idem = re.sub(r'[^A-Z]+', '_', options.headerfilename.upper())
if options.printwire:
    if options.header:
        template = print_header_template
    else:
        template = print_template
elif options.header:
    template = header_template
else:
    template = impl_template

# Print out all the things
toplevel_messages = [m for m in messages if not m.is_tlv]
built_hdr_enums = build_hdr_enums(options.enumname, toplevel_messages, tlv_fields)
built_impl_enums = build_impl_enums(options.enumname, toplevel_messages, tlv_fields)
tlv_structs = build_tlv_structs(tlv_fields)
tlv_structs += build_tlv_type_structs(tlv_fields)
includes = '\n'.join(includes)
printcases = ['case {enum.name}: printf("{enum.name}:\\n"); printwire_{name}("{name}", msg); return;'.format(enum=m.enum, name=m.name) for m in toplevel_messages]

if options.printwire:
    decls = [m.print_printwire(options.header) for m in messages + messages_with_option]
else:
    towire_decls = []
    fromwire_decls = []

    for tlv_field, tlv_messages in tlv_fields.items():
        for m in tlv_messages:
            towire_decls.append(m.print_towire(options.header, tlv_field))
            fromwire_decls.append(m.print_fromwire(options.header, tlv_field))

    if not options.header:
        tlv_towires = build_tlv_towires(tlv_fields)
        tlv_fromwires = build_tlv_fromwires(tlv_fields)
        towire_decls += tlv_towires
        fromwire_decls += tlv_fromwires

    towire_decls += [m.print_towire(options.header, '') for m in messages + messages_with_option]
    fromwire_decls += [m.print_fromwire(options.header, '') for m in messages + messages_with_option]
    decls = fromwire_decls + towire_decls

print(template.format(
    headerfilename=options.headerfilename,
    printcases='\n\t'.join(printcases),
    idem=idem,
    includes=includes,
    enumname=options.enumname,
    formatted_hdr_enums=built_hdr_enums,
    formatted_impl_enums=built_impl_enums,
    tlv_structs=tlv_structs,
    func_decls='\n'.join(decls)))
