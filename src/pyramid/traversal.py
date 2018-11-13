from functools import lru_cache
from zope.interface import implementer
from zope.interface.interfaces import IInterface

from pyramid.interfaces import (
    IResourceURL,
    IRequestFactory,
    ITraverser,
    VH_ROOT_KEY,
)

from pyramid.compat import (
    native_,
    ascii_native_,
    is_nonstr_iter,
    decode_path_info,
    unquote_bytes_to_wsgi,
)

from pyramid.encode import url_quote
from pyramid.exceptions import URLDecodeError
from pyramid.location import lineage
from pyramid.threadlocal import get_current_registry

PATH_SEGMENT_SAFE = "~!$&'()*+,;=:@"  # from webob
PATH_SAFE = PATH_SEGMENT_SAFE + "/"

empty = ''


def find_root(resource):
    """ Find the root node in the resource tree to which ``resource``
    belongs. Note that ``resource`` should be :term:`location`-aware.
    Note that the root resource is available in the request object by
    accessing the ``request.root`` attribute.
    """
    for location in lineage(resource):
        if location.__parent__ is None:
            resource = location
            break
    return resource


def find_resource(resource, path):
    """ Given a resource object and a string or tuple representing a path
    (such as the return value of :func:`pyramid.traversal.resource_path` or
    :func:`pyramid.traversal.resource_path_tuple`), return a resource in this
    application's resource tree at the specified path.  The resource passed
    in *must* be :term:`location`-aware.  If the path cannot be resolved (if
    the respective node in the resource tree does not exist), a
    :exc:`KeyError` will be raised.

    This function is the logical inverse of
    :func:`pyramid.traversal.resource_path` and
    :func:`pyramid.traversal.resource_path_tuple`; it can resolve any
    path string or tuple generated by either of those functions.

    Rules for passing a *string* as the ``path`` argument: if the
    first character in the path string is the ``/``
    character, the path is considered absolute and the resource tree
    traversal will start at the root resource.  If the first character
    of the path string is *not* the ``/`` character, the path is
    considered relative and resource tree traversal will begin at the resource
    object supplied to the function as the ``resource`` argument.  If an
    empty string is passed as ``path``, the ``resource`` passed in will
    be returned.  Resource path strings must be escaped in the following
    manner: each Unicode path segment must be encoded as UTF-8 and as
    each path segment must escaped via Python's :mod:`urllib.quote`.
    For example, ``/path/to%20the/La%20Pe%C3%B1a`` (absolute) or
    ``to%20the/La%20Pe%C3%B1a`` (relative).  The
    :func:`pyramid.traversal.resource_path` function generates strings
    which follow these rules (albeit only absolute ones).

    Rules for passing *text* (Unicode) as the ``path`` argument are the same
    as those for a string.  In particular, the text may not have any nonascii
    characters in it.

    Rules for passing a *tuple* as the ``path`` argument: if the first
    element in the path tuple is the empty string (for example ``('',
    'a', 'b', 'c')``, the path is considered absolute and the resource tree
    traversal will start at the resource tree root object.  If the first
    element in the path tuple is not the empty string (for example
    ``('a', 'b', 'c')``), the path is considered relative and resource tree
    traversal will begin at the resource object supplied to the function
    as the ``resource`` argument.  If an empty sequence is passed as
    ``path``, the ``resource`` passed in itself will be returned.  No
    URL-quoting or UTF-8-encoding of individual path segments within
    the tuple is required (each segment may be any string or unicode
    object representing a resource name).  Resource path tuples generated by
    :func:`pyramid.traversal.resource_path_tuple` can always be
    resolved by ``find_resource``.
    """
    if isinstance(path, str):
        path = ascii_native_(path)
    D = traverse(resource, path)
    view_name = D['view_name']
    context = D['context']
    if view_name:
        raise KeyError('%r has no subelement %s' % (context, view_name))
    return context


find_model = find_resource  # b/w compat (forever)


def find_interface(resource, class_or_interface):
    """
    Return the first resource found in the :term:`lineage` of ``resource``
    which, a) if ``class_or_interface`` is a Python class object, is an
    instance of the class or any subclass of that class or b) if
    ``class_or_interface`` is a :term:`interface`, provides the specified
    interface.  Return ``None`` if no resource providing ``interface_or_class``
    can be found in the lineage.  The ``resource`` passed in *must* be
    :term:`location`-aware.
    """
    if IInterface.providedBy(class_or_interface):
        test = class_or_interface.providedBy
    else:
        test = lambda arg: isinstance(arg, class_or_interface)
    for location in lineage(resource):
        if test(location):
            return location


def resource_path(resource, *elements):
    """ Return a string object representing the absolute physical path of the
    resource object based on its position in the resource tree, e.g
    ``/foo/bar``.  Any positional arguments passed in as ``elements`` will be
    appended as path segments to the end of the resource path.  For instance,
    if the resource's path is ``/foo/bar`` and ``elements`` equals ``('a',
    'b')``, the returned string will be ``/foo/bar/a/b``.  The first
    character in the string will always be the ``/`` character (a leading
    ``/`` character in a path string represents that the path is absolute).

    Resource path strings returned will be escaped in the following
    manner: each unicode path segment will be encoded as UTF-8 and
    each path segment will be escaped via Python's :mod:`urllib.quote`.
    For example, ``/path/to%20the/La%20Pe%C3%B1a``.

    This function is a logical inverse of
    :mod:`pyramid.traversal.find_resource`: it can be used to generate
    path references that can later be resolved via that function.

    The ``resource`` passed in *must* be :term:`location`-aware.

    .. note::

       Each segment in the path string returned will use the ``__name__``
       attribute of the resource it represents within the resource tree.  Each
       of these segments *should* be a unicode or string object (as per the
       contract of :term:`location`-awareness).  However, no conversion or
       safety checking of resource names is performed.  For instance, if one of
       the resources in your tree has a ``__name__`` which (by error) is a
       dictionary, the :func:`pyramid.traversal.resource_path` function will
       attempt to append it to a string and it will cause a
       :exc:`pyramid.exceptions.URLDecodeError`.

    .. note::

       The :term:`root` resource *must* have a ``__name__`` attribute with a
       value of either ``None`` or the empty string for paths to be generated
       properly.  If the root resource has a non-null ``__name__`` attribute,
       its name will be prepended to the generated path rather than a single
       leading '/' character.
    """
    # joining strings is a bit expensive so we delegate to a function
    # which caches the joined result for us
    return _join_path_tuple(resource_path_tuple(resource, *elements))


model_path = resource_path  # b/w compat (forever)


def traverse(resource, path):
    """Given a resource object as ``resource`` and a string or tuple
    representing a path as ``path`` (such as the return value of
    :func:`pyramid.traversal.resource_path` or
    :func:`pyramid.traversal.resource_path_tuple` or the value of
    ``request.environ['PATH_INFO']``), return a dictionary with the
    keys ``context``, ``root``, ``view_name``, ``subpath``,
    ``traversed``, ``virtual_root``, and ``virtual_root_path``.

    A definition of each value in the returned dictionary:

    - ``context``: The :term:`context` (a :term:`resource` object) found
      via traversal or url dispatch.  If the ``path`` passed in is the
      empty string, the value of the ``resource`` argument passed to this
      function is returned.

    - ``root``: The resource object at which :term:`traversal` begins.
      If the ``resource`` passed in was found via url dispatch or if the
      ``path`` passed in was relative (non-absolute), the value of the
      ``resource`` argument passed to this function is returned.

    - ``view_name``: The :term:`view name` found during
      :term:`traversal` or :term:`url dispatch`; if the ``resource`` was
      found via traversal, this is usually a representation of the
      path segment which directly follows the path to the ``context``
      in the ``path``.  The ``view_name`` will be a Unicode object or
      the empty string.  The ``view_name`` will be the empty string if
      there is no element which follows the ``context`` path.  An
      example: if the path passed is ``/foo/bar``, and a resource
      object is found at ``/foo`` (but not at ``/foo/bar``), the 'view
      name' will be ``u'bar'``.  If the ``resource`` was found via
      urldispatch, the view_name will be the name the route found was
      registered with.

    - ``subpath``: For a ``resource`` found via :term:`traversal`, this
      is a sequence of path segments found in the ``path`` that follow
      the ``view_name`` (if any).  Each of these items is a Unicode
      object.  If no path segments follow the ``view_name``, the
      subpath will be the empty sequence.  An example: if the path
      passed is ``/foo/bar/baz/buz``, and a resource object is found at
      ``/foo`` (but not ``/foo/bar``), the 'view name' will be
      ``u'bar'`` and the :term:`subpath` will be ``[u'baz', u'buz']``.
      For a ``resource`` found via url dispatch, the subpath will be a
      sequence of values discerned from ``*subpath`` in the route
      pattern matched or the empty sequence.

    - ``traversed``: The sequence of path elements traversed from the
      root to find the ``context`` object during :term:`traversal`.
      Each of these items is a Unicode object.  If no path segments
      were traversed to find the ``context`` object (e.g. if the
      ``path`` provided is the empty string), the ``traversed`` value
      will be the empty sequence.  If the ``resource`` is a resource found
      via :term:`url dispatch`, traversed will be None.

    - ``virtual_root``: A resource object representing the 'virtual' root
      of the resource tree being traversed during :term:`traversal`.
      See :ref:`vhosting_chapter` for a definition of the virtual root
      object.  If no virtual hosting is in effect, and the ``path``
      passed in was absolute, the ``virtual_root`` will be the
      *physical* root resource object (the object at which :term:`traversal`
      begins).  If the ``resource`` passed in was found via :term:`URL
      dispatch` or if the ``path`` passed in was relative, the
      ``virtual_root`` will always equal the ``root`` object (the
      resource passed in).

    - ``virtual_root_path`` -- If :term:`traversal` was used to find
      the ``resource``, this will be the sequence of path elements
      traversed to find the ``virtual_root`` resource.  Each of these
      items is a Unicode object.  If no path segments were traversed
      to find the ``virtual_root`` resource (e.g. if virtual hosting is
      not in effect), the ``traversed`` value will be the empty list.
      If url dispatch was used to find the ``resource``, this will be
      ``None``.

    If the path cannot be resolved, a :exc:`KeyError` will be raised.

    Rules for passing a *string* as the ``path`` argument: if the
    first character in the path string is the with the ``/``
    character, the path will considered absolute and the resource tree
    traversal will start at the root resource.  If the first character
    of the path string is *not* the ``/`` character, the path is
    considered relative and resource tree traversal will begin at the resource
    object supplied to the function as the ``resource`` argument.  If an
    empty string is passed as ``path``, the ``resource`` passed in will
    be returned.  Resource path strings must be escaped in the following
    manner: each Unicode path segment must be encoded as UTF-8 and
    each path segment must escaped via Python's :mod:`urllib.quote`.
    For example, ``/path/to%20the/La%20Pe%C3%B1a`` (absolute) or
    ``to%20the/La%20Pe%C3%B1a`` (relative).  The
    :func:`pyramid.traversal.resource_path` function generates strings
    which follow these rules (albeit only absolute ones).

    Rules for passing a *tuple* as the ``path`` argument: if the first
    element in the path tuple is the empty string (for example ``('',
    'a', 'b', 'c')``, the path is considered absolute and the resource tree
    traversal will start at the resource tree root object.  If the first
    element in the path tuple is not the empty string (for example
    ``('a', 'b', 'c')``), the path is considered relative and resource tree
    traversal will begin at the resource object supplied to the function
    as the ``resource`` argument.  If an empty sequence is passed as
    ``path``, the ``resource`` passed in itself will be returned.  No
    URL-quoting or UTF-8-encoding of individual path segments within
    the tuple is required (each segment may be any string or unicode
    object representing a resource name).

    Explanation of the conversion of ``path`` segment values to
    Unicode during traversal: Each segment is URL-unquoted, and
    decoded into Unicode. Each segment is assumed to be encoded using
    the UTF-8 encoding (or a subset, such as ASCII); a
    :exc:`pyramid.exceptions.URLDecodeError` is raised if a segment
    cannot be decoded.  If a segment name is empty or if it is ``.``,
    it is ignored.  If a segment name is ``..``, the previous segment
    is deleted, and the ``..`` is ignored.  As a result of this
    process, the return values ``view_name``, each element in the
    ``subpath``, each element in ``traversed``, and each element in
    the ``virtual_root_path`` will be Unicode as opposed to a string,
    and will be URL-decoded.
    """

    if is_nonstr_iter(path):
        # the traverser factory expects PATH_INFO to be a string, not
        # unicode and it expects path segments to be utf-8 and
        # urlencoded (it's the same traverser which accepts PATH_INFO
        # from user agents; user agents always send strings).
        if path:
            path = _join_path_tuple(tuple(path))
        else:
            path = ''

    # The user is supposed to pass us a string object, never Unicode.  In
    # practice, however, users indeed pass Unicode to this API.  If they do
    # pass a Unicode object, its data *must* be entirely encodeable to ASCII,
    # so we encode it here as a convenience to the user and to prevent
    # second-order failures from cropping up (all failures will occur at this
    # step rather than later down the line as the result of calling
    # ``traversal_path``).

    path = ascii_native_(path)

    if path and path[0] == '/':
        resource = find_root(resource)

    reg = get_current_registry()

    request_factory = reg.queryUtility(IRequestFactory)
    if request_factory is None:
        from pyramid.request import Request  # avoid circdep

        request_factory = Request

    request = request_factory.blank(path)
    request.registry = reg
    traverser = reg.queryAdapter(resource, ITraverser)
    if traverser is None:
        traverser = ResourceTreeTraverser(resource)

    return traverser(request)


def resource_path_tuple(resource, *elements):
    """
    Return a tuple representing the absolute physical path of the
    ``resource`` object based on its position in a resource tree, e.g
    ``('', 'foo', 'bar')``.  Any positional arguments passed in as
    ``elements`` will be appended as elements in the tuple
    representing the resource path.  For instance, if the resource's
    path is ``('', 'foo', 'bar')`` and elements equals ``('a', 'b')``,
    the returned tuple will be ``('', 'foo', 'bar', 'a', 'b')``.  The
    first element of this tuple will always be the empty string (a
    leading empty string element in a path tuple represents that the
    path is absolute).

    This function is a logical inverse of
    :func:`pyramid.traversal.find_resource`: it can be used to
    generate path references that can later be resolved by that function.

    The ``resource`` passed in *must* be :term:`location`-aware.

    .. note::

       Each segment in the path tuple returned will equal the ``__name__``
       attribute of the resource it represents within the resource tree.  Each
       of these segments *should* be a unicode or string object (as per the
       contract of :term:`location`-awareness).  However, no conversion or
       safety checking of resource names is performed.  For instance, if one of
       the resources in your tree has a ``__name__`` which (by error) is a
       dictionary, that dictionary will be placed in the path tuple; no warning
       or error will be given.

    .. note::

       The :term:`root` resource *must* have a ``__name__`` attribute with a
       value of either ``None`` or the empty string for path tuples to be
       generated properly.  If the root resource has a non-null ``__name__``
       attribute, its name will be the first element in the generated path
       tuple rather than the empty string.
    """
    return tuple(_resource_path_list(resource, *elements))


model_path_tuple = resource_path_tuple  # b/w compat (forever)


def _resource_path_list(resource, *elements):
    """ Implementation detail shared by resource_path and
    resource_path_tuple"""
    path = [loc.__name__ or '' for loc in lineage(resource)]
    path.reverse()
    path.extend(elements)
    return path


_model_path_list = _resource_path_list  # b/w compat, not an API


def virtual_root(resource, request):
    """
    Provided any :term:`resource` and a :term:`request` object, return
    the resource object representing the :term:`virtual root` of the
    current :term:`request`.  Using a virtual root in a
    :term:`traversal` -based :app:`Pyramid` application permits
    rooting. For example, the resource at the traversal path ``/cms`` will
    be found at ``http://example.com/`` instead of rooting it at
    ``http://example.com/cms/``.

    If the ``resource`` passed in is a context obtained via
    :term:`traversal`, and if the ``HTTP_X_VHM_ROOT`` key is in the
    WSGI environment, the value of this key will be treated as a
    'virtual root path': the :func:`pyramid.traversal.find_resource`
    API will be used to find the virtual root resource using this path;
    if the resource is found, it will be returned.  If the
    ``HTTP_X_VHM_ROOT`` key is not present in the WSGI environment,
    the physical :term:`root` of the resource tree will be returned instead.

    Virtual roots are not useful at all in applications that use
    :term:`URL dispatch`. Contexts obtained via URL dispatch don't
    really support being virtually rooted (each URL dispatch context
    is both its own physical and virtual root).  However if this API
    is called with a ``resource`` argument which is a context obtained
    via URL dispatch, the resource passed in will be returned
    unconditionally."""
    try:
        reg = request.registry
    except AttributeError:
        reg = get_current_registry()
    url_adapter = reg.queryMultiAdapter((resource, request), IResourceURL)
    if url_adapter is None:
        url_adapter = ResourceURL(resource, request)

    vpath, rpath = url_adapter.virtual_path, url_adapter.physical_path
    if rpath != vpath and rpath.endswith(vpath):
        vroot_path = rpath[: -len(vpath)]
        return find_resource(resource, vroot_path)

    try:
        return request.root
    except AttributeError:
        return find_root(resource)


def traversal_path(path):
    """ Variant of :func:`pyramid.traversal.traversal_path_info` suitable for
    decoding paths that are URL-encoded.

    If this function is passed a Unicode object instead of a sequence of
    bytes as ``path``, that Unicode object *must* directly encodeable to
    ASCII.  For example, u'/foo' will work but u'/<unprintable unicode>' (a
    Unicode object with characters that cannot be encoded to ascii) will
    not. A :exc:`UnicodeEncodeError` will be raised if the Unicode cannot be
    encoded directly to ASCII.
    """
    if isinstance(path, str):
        # must not possess characters outside ascii
        path = path.encode('ascii')
    # we unquote this path exactly like a PEP 3333 server would
    path = unquote_bytes_to_wsgi(path)  # result will be a native string
    return traversal_path_info(path)  # result will be a tuple of unicode


@lru_cache(1000)
def traversal_path_info(path):
    """ Given``path``, return a tuple representing that path which can be
    used to traverse a resource tree.  ``path`` is assumed to be an
    already-URL-decoded ``str`` type as if it had come to us from an upstream
    WSGI server as the ``PATH_INFO`` environ variable.

    The ``path`` is first decoded to from its WSGI representation to Unicode;
    it is decoded differently depending on platform:

    - On Python 2, ``path`` is decoded to Unicode from bytes using the UTF-8
      decoding directly; a :exc:`pyramid.exc.URLDecodeError` is raised if a the
      URL cannot be decoded.

    - On Python 3, as per the PEP 3333 spec, ``path`` is first encoded to
      bytes using the Latin-1 encoding; the resulting set of bytes is
      subsequently decoded to text using the UTF-8 encoding; a
      :exc:`pyramid.exc.URLDecodeError` is raised if a the URL cannot be
      decoded.

    The ``path`` is split on slashes, creating a list of segments.  If a
    segment name is empty or if it is ``.``, it is ignored.  If a segment
    name is ``..``, the previous segment is deleted, and the ``..`` is
    ignored.

    Examples:

    ``/``

        ()

    ``/foo/bar/baz``

        (u'foo', u'bar', u'baz')

    ``foo/bar/baz``

        (u'foo', u'bar', u'baz')

    ``/foo/bar/baz/``

        (u'foo', u'bar', u'baz')

    ``/foo//bar//baz/``

        (u'foo', u'bar', u'baz')

    ``/foo/bar/baz/..``

        (u'foo', u'bar')

    ``/my%20archives/hello``

        (u'my archives', u'hello')

    ``/archives/La%20Pe%C3%B1a``

        (u'archives', u'<unprintable unicode>')

    .. note::

      This function does not generate the same type of tuples that
      :func:`pyramid.traversal.resource_path_tuple` does.  In particular, the
      leading empty string is not present in the tuple it returns, unlike
      tuples returned by :func:`pyramid.traversal.resource_path_tuple`.  As a
      result, tuples generated by ``traversal_path`` are not resolveable by
      the :func:`pyramid.traversal.find_resource` API.  ``traversal_path`` is
      a function mostly used by the internals of :app:`Pyramid` and by people
      writing their own traversal machinery, as opposed to users writing
      applications in :app:`Pyramid`.
    """
    try:
        path = decode_path_info(path)  # result will be Unicode
    except UnicodeDecodeError as e:
        raise URLDecodeError(e.encoding, e.object, e.start, e.end, e.reason)
    return split_path_info(path)  # result will be tuple of Unicode


@lru_cache(1000)
def split_path_info(path):
    # suitable for splitting an already-unquoted-already-decoded (unicode)
    # path value
    path = path.strip('/')
    clean = []
    for segment in path.split('/'):
        if not segment or segment == '.':
            continue
        elif segment == '..':
            if clean:
                del clean[-1]
        else:
            clean.append(segment)
    return tuple(clean)


_segment_cache = {}


def quote_path_segment(segment, safe=PATH_SEGMENT_SAFE):
    """
    Return a quoted representation of a 'path segment' (such as
    the string ``__name__`` attribute of a resource) as a string.  If the
    ``segment`` passed in is a unicode object, it is converted to a
    UTF-8 string, then it is URL-quoted using Python's
    ``urllib.quote``.  If the ``segment`` passed in is a string, it is
    URL-quoted using Python's :mod:`urllib.quote`.  If the segment
    passed in is not a string or unicode object, an error will be
    raised.  The return value of ``quote_path_segment`` is always a
    string, never Unicode.

    You may pass a string of characters that need not be encoded as
    the ``safe`` argument to this function.  This corresponds to the
    ``safe`` argument to :mod:`urllib.quote`.

    .. note::

       The return value for each segment passed to this
       function is cached in a module-scope dictionary for
       speed: the cached version is returned when possible
       rather than recomputing the quoted version.  No cache
       emptying is ever done for the lifetime of an
       application, however.  If you pass arbitrary
       user-supplied strings to this function (as opposed to
       some bounded set of values from a 'working set' known to
       your application), it may become a memory leak.

    """
    # The bit of this code that deals with ``_segment_cache`` is an
    # optimization: we cache all the computation of URL path segments
    # in this module-scope dictionary with the original string (or
    # unicode value) as the key, so we can look it up later without
    # needing to reencode or re-url-quote it
    try:
        return _segment_cache[(segment, safe)]
    except KeyError:
        if segment.__class__ not in (str, bytes):
            segment = str(segment)
        result = url_quote(native_(segment, 'utf-8'), safe)
        # we don't need a lock to mutate _segment_cache, as the below
        # will generate exactly one Python bytecode (STORE_SUBSCR)
        _segment_cache[(segment, safe)] = result
        return result


slash = '/'


@implementer(ITraverser)
class ResourceTreeTraverser(object):
    """ A resource tree traverser that should be used (for speed) when
    every resource in the tree supplies a ``__name__`` and
    ``__parent__`` attribute (ie. every resource in the tree is
    :term:`location` aware) ."""

    VH_ROOT_KEY = VH_ROOT_KEY
    VIEW_SELECTOR = '@@'

    def __init__(self, root):
        self.root = root

    def __call__(self, request):
        environ = request.environ
        matchdict = request.matchdict

        if matchdict is not None:

            path = matchdict.get('traverse', slash) or slash
            if is_nonstr_iter(path):
                # this is a *traverse stararg (not a {traverse})
                # routing has already decoded these elements, so we just
                # need to join them
                path = '/' + slash.join(path) or slash

            subpath = matchdict.get('subpath', ())
            if not is_nonstr_iter(subpath):
                # this is not a *subpath stararg (just a {subpath})
                # routing has already decoded this string, so we just need
                # to split it
                subpath = split_path_info(subpath)

        else:
            # this request did not match a route
            subpath = ()
            try:
                # empty if mounted under a path in mod_wsgi, for example
                path = request.path_info or slash
            except KeyError:
                # if environ['PATH_INFO'] is just not there
                path = slash
            except UnicodeDecodeError as e:
                raise URLDecodeError(
                    e.encoding, e.object, e.start, e.end, e.reason
                )

        if self.VH_ROOT_KEY in environ:
            # HTTP_X_VHM_ROOT
            vroot_path = decode_path_info(environ[self.VH_ROOT_KEY])
            vroot_tuple = split_path_info(vroot_path)
            vpath = (
                vroot_path + path
            )  # both will (must) be unicode or asciistr
            vroot_idx = len(vroot_tuple) - 1
        else:
            vroot_tuple = ()
            vpath = path
            vroot_idx = -1

        root = self.root
        ob = vroot = root

        if vpath == slash:  # invariant: vpath must not be empty
            # prevent a call to traversal_path if we know it's going
            # to return the empty tuple
            vpath_tuple = ()
        else:
            # we do dead reckoning here via tuple slicing instead of
            # pushing and popping temporary lists for speed purposes
            # and this hurts readability; apologies
            i = 0
            view_selector = self.VIEW_SELECTOR
            vpath_tuple = split_path_info(vpath)
            for segment in vpath_tuple:
                if segment[:2] == view_selector:
                    return {
                        'context': ob,
                        'view_name': segment[2:],
                        'subpath': vpath_tuple[i + 1 :],
                        'traversed': vpath_tuple[: vroot_idx + i + 1],
                        'virtual_root': vroot,
                        'virtual_root_path': vroot_tuple,
                        'root': root,
                    }
                try:
                    getitem = ob.__getitem__
                except AttributeError:
                    return {
                        'context': ob,
                        'view_name': segment,
                        'subpath': vpath_tuple[i + 1 :],
                        'traversed': vpath_tuple[: vroot_idx + i + 1],
                        'virtual_root': vroot,
                        'virtual_root_path': vroot_tuple,
                        'root': root,
                    }

                try:
                    next = getitem(segment)
                except KeyError:
                    return {
                        'context': ob,
                        'view_name': segment,
                        'subpath': vpath_tuple[i + 1 :],
                        'traversed': vpath_tuple[: vroot_idx + i + 1],
                        'virtual_root': vroot,
                        'virtual_root_path': vroot_tuple,
                        'root': root,
                    }
                if i == vroot_idx:
                    vroot = next
                ob = next
                i += 1

        return {
            'context': ob,
            'view_name': empty,
            'subpath': subpath,
            'traversed': vpath_tuple,
            'virtual_root': vroot,
            'virtual_root_path': vroot_tuple,
            'root': root,
        }


ModelGraphTraverser = (
    ResourceTreeTraverser
)  # b/w compat, not API, used in wild


@implementer(IResourceURL)
class ResourceURL(object):
    VH_ROOT_KEY = VH_ROOT_KEY

    def __init__(self, resource, request):
        physical_path_tuple = resource_path_tuple(resource)
        physical_path = _join_path_tuple(physical_path_tuple)

        if physical_path_tuple != ('',):
            physical_path_tuple = physical_path_tuple + ('',)
            physical_path = physical_path + '/'

        virtual_path = physical_path
        virtual_path_tuple = physical_path_tuple

        environ = request.environ
        vroot_path = environ.get(self.VH_ROOT_KEY)

        # if the physical path starts with the virtual root path, trim it out
        # of the virtual path
        if vroot_path is not None:
            vroot_path = vroot_path.rstrip('/')
            if vroot_path and physical_path.startswith(vroot_path):
                vroot_path_tuple = tuple(vroot_path.split('/'))
                numels = len(vroot_path_tuple)
                virtual_path_tuple = ('',) + physical_path_tuple[numels:]
                virtual_path = physical_path[len(vroot_path) :]

        self.virtual_path = virtual_path  # IResourceURL attr
        self.physical_path = physical_path  # IResourceURL attr
        self.virtual_path_tuple = virtual_path_tuple  # IResourceURL attr (1.5)
        self.physical_path_tuple = (
            physical_path_tuple
        )  # IResourceURL attr (1.5)


@lru_cache(1000)
def _join_path_tuple(tuple):
    return tuple and '/'.join([quote_path_segment(x) for x in tuple]) or '/'


class DefaultRootFactory:
    __parent__ = None
    __name__ = None

    def __init__(self, request):
        pass
