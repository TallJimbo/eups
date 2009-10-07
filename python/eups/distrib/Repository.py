"""
the Repository class -- An interface into a distribution server for 
installing and deploying distribution packages.
"""
import sys, os, re, atexit, shutil
import eups
import server 
from eups.tags      import Tag, TagNotRecognized
from eups.utils     import Flavor, Quiet
from server         import ServerConf, Manifest, TaggedProductList
from server         import RemoteFileNotFound, LocalTransporter
from DistribFactory import DistribFactory
from Distrib        import DefaultDistrib, findInstallableRoot

class Repository(object):
    """
    an interface into a distribution server for handling package 
    installation and creation requests.  

    Applications should use install packages via the Repositories class
    (via its install() function) because not only does it take responsibility 
    for installing a product's dependencies as well, it has the ability to 
    search across multiple servers to find a package.  In contrast, a 
    Repository handles installation of a single product; this is accomplished
    via a Distrib instance returned by getDistribFor().  

    If isWritable() returns True, this package can create 
    """

    def __init__(self, eupsenv, pkgroot, flavor=None, options=None, 
                 distFactory=None, verbosity=0, log=sys.stderr):
        """
        create a Repository for a given server base URL (pkgroot)
        @param eupsenv       the Eups controller instance to use
        @param pkgroot       the base URL for the package server to pull 
                                packages from
        @param flavor        the platform flavor of interest.  
                                #--CUT
                                When installing
                                packages, this value is ignored and the version
                                set in the Eups controller is assumed to be
                                the target platform.  For all other actions
                                (creating server packages, listing available 
                                packages), this value will be assumed.  
                                #--
                                If 
                                None or "generic", then a generic platform
                                is assumed. 
        @param options       a dictionary of options to pass to Distrib 
                                instances used to install and create packages
        @param distFactory   a DistFactory instance to use.  If not provided
                                a default one is created.
        @param verbosity     if > 0, print status messages; the higher the 
                               number, the more messages that are printed
                               (default=0).
        @param log           the destination for status messages (default:
                               sys.stderr)
        """
        self.eups = eupsenv
        if not flavor:
            flavor = self.eups.flavor
        self.flavor = flavor
        self.distFactory = None
        if distFactory:
            self.distFactory = distFactory.clone()
        self.options = options
        self.verbose = verbosity
        if not isinstance(self.verbose, int):
            self.verbose = 0
        self.log = log
        self.distServer = None
        if self.options is None:
            self.options = {}
        if not isinstance(self.options, dict):
            raise RuntimeError("Non-dictionary passed to options parameter: " +
                               repr(self.options))
        self.pkgroot = pkgroot
        if pkgroot:
            override = None
            if self.options.has_key('serverconf'):
                override = options['serverconf']
            self.distServer = ServerConf.makeServer(pkgroot, eupsenv=eupsenv,
                                                    override=override,
                                                    verbosity=self.verbose-1, 
                                                    log=self.log)
        if self.distFactory is None:
            self.distFactory = DistribFactory(self.eups, self.distServer)
        elif not self.distServer:
            self.distFactory.resetDistribServer(self.distServer)

        # a cache of the supported tag names
        self._supportedTags = None

        # a cache of supported packages
        self._pkgList = None

        # True if servers should always be queried when looking for a 
        # repository to get a package from.  If False, an internal cache
        # of available products will be used.
        self._alwaysQueryServer = False
        if self.options.has_key("alwaysQueryServer"):
            if isinstance(self.options["alwaysQueryServer"], str):
                self.options["alwaysQueryServer"] = \
                    self.options["alwaysQueryServer"].upper()
                if "TRUE".startswith(self.options["alwaysQueryServer"]):
                    self.options["alwaysQueryServer"] = True
                else:
                    self.options["alwaysQueryServer"] = False
            if self.options["alwaysQueryServer"]:
                self._alwaysQueryServer = True

    def _mergeOptions(self, override):
        if self.options:
            out = self.options.copy()
        else:
            out = {}
        if isinstance(override, dict):
            for key in override.keys():
                out[key] = self.override[key]
            if len(out.keys()) == 0:
                return None
        return out

    def _getPackageLookup(self):
        pkgs = self.distServer.listAvailableProducts()

        # arrange into a hierarchical lookup
        #
        lookup = {}  # key is product
        for pkg in pkgs:
            if not lookup.has_key(pkg[0]):   
                lookup[pkg[0]] = {}                # key is flavor
            if not lookup[pkg[0]].has_key(pkg[2]):
                lookup[pkg[0]][pkg[2]] = []        # list of versions
            lookup[pkg[0]][pkg[2]].append(pkg[1])

        # now sort the contents
        keys = lookup.keys()
        keys.sort()
        lookup["_sortOrder"] = keys

        for prod in lookup["_sortOrder"]:
            keys = filter(lambda f: f != "generic", lookup[prod].keys())
            keys.sort()
            if "generic" in lookup[prod].keys():
                keys.insert(0, "generic")
            lookup[prod]["_sortOrder"] = keys

            for flav in lookup[prod]["_sortOrder"]:
                lookup[prod][flav].sort(self.eups.version_cmp)

        return lookup

    def getTagNamesFor(self, product, version, flavor="generic", 
                       tags=None, noaction=False):
        """
        return as a list of strings all of the tag names assigned to 
        the given product by the repository.
        @param product     the name of the product
        @param version     the product's version
        @param flavor      the platform flavor (default: generic)
        @param tags        if set, the returned list will be the intersection
                             of the tags assigned by the server with this list.
                             By providing this, one can remove the need to 
                             query the server for its list of supported tags.  
        """
        return self.distServer.getTagNamesFor(product, version, flavor, tags)

    def getManifest(self, product, version, flavor, noaction=False):
        """
        request the manifest for a particular product and return it as 
        a Manifest instance.
        @param product     the desired product name
        @param version     the desired version of the product
        @param flavor      the flavor of the target platform
        """
        return self.distServer.getManifest(product, version, flavor, noaction)

    def findPackage(self, product, version=None, prefFlavors=None):
        """
        if this repository can provide a requesed package, return a tuple 
        of (product, version, flavor) reflecting the exact version of the 
        product available.  If it is not available, return None.
        @param product     the name of the product
        @param version     the desired version.  This can either be a version
                             string or an instance of Tag.  If None, 
                             the tags preferred by the Eups environment will
                             be searched.  
        @param prefFlavors the preferred platform flavors in an ordered list.  
                             A single flavor may be given as a string.  If None, 
                             flavors preferred by the Eups environment will
                             be searched.  
        """
        if prefFlavors is None:
            prefFlavors = Flavor().getFallbackFlavors(self.flavor, True)
        elif not isinstance(prefFlavors, list):
            prefFlavors = [prefFlavors]
            
        versions = [version]
        supportedTags = self.getSupportedTags() + ["newest"]
        if version and isinstance(version, Tag):
            if not version.isGlobal():
                raise TagNotRecognized(tag.name, "global", 
                                       msg="Non-global tag %s requested." % 
                                           version.name)
        if not version:
            versions = self.eups.getPreferredTags()

        for vers in versions:
            if isinstance(vers, Tag) and \
               vers.name not in supportedTags:
                if self.verbose > 0:
                    print >> self.log, \
                        "Tag %s not supported at %s" % (vers.name, self.pkgroot)
                continue

            for flavor in prefFlavors:
                try:
                    out = self.listPackages(product, version, flavor)
                    if out:  return out[0]
                except TagNotRecognized:
                    pass

        return None

    def getSupportedTags(self):
        """
        return a list of the tag names supported by this repository
        """
        if self._supportedTags is None:
            self._supportedTags = self.distServer.getTagNames()
        return self._supportedTags

    def listPackages(self, product=None, version=None, flavor=None,
                     queryServer=None, noaction=False):
        """
        return a list of available products on the server.  Each item 
        in the list is a list of the form, (product, version, flavor).
        The optional inputs will restrict the list to those matching the 
        values.  
        @param product     the name of the product.  If None, all available 
                             products are returned.
        @param version     the desired version.  This can either be a version
                             version string or an instance of Tag.  If None, 
                             all available versions are returned.
        @param flavor      the desired platform flavor.  If None, all 
                             available versions are returned.
        @param queryServer if True, this will force a query to the repository
                             server.  If False, an internal cache will be used
                             if possible.  If None (default), the behavior is 
                             controlled by the "alwaysQueryServer" passed to 
                             the constructor of this Repository (which defaults
                             to False).  
        """
        if queryServer is None:
            queryServer = self._alwaysQueryServer

        if queryServer or \
           (isinstance(version, Tag) and version.name != "newest"):

            if self.distServer is None:
                raise RuntimeError("No distribution server set")

            tag = None
            if version and isinstance(version, Tag):
                if not version.isGlobal():
                    raise TagNotRecognized(tag.name, "global", 
                                           msg="Non-global tag %s requested." % 
                                               version.name)
                tag = version.name
                version = None

                if tag == "newest":
                    return self._listNewestProducts(product, flavor)

                if tag not in self.getSupportedTags():
                    raise TagNotRecognized(tag, "global", 
                                           msg="tag %s not supported by server."
                                               % tag)

            return self.distServer.listAvailableProducts(product, version, 
                                                         flavor, tag)

        else:
            if self._pkgList is None:
                self._pkgList = self._getPackageLookup()
            out = []

            prods = self._pkgList["_sortOrder"]
            if product:
                if not self._pkgList.has_key(product):
                    return []
                prods = [product]

            for prod in prods:
                flavs = self._pkgList[prod]["_sortOrder"]
                if flavor:
                    if not self._pkgList[prod].has_key(flavor):
                        continue
                    flavs = [flavor]

                for flav in flavs:
                    if version is None:
                        out.extend( map(lambda v: (prod, v, flav), 
                                    self._pkgList[prod][flav]) )
                    elif version and isinstance(version, str):
                        if version not in self._pkgList[prod][flav]:
                            continue
                        out.append( (prod, version, flav) )
                    else:
                        # looking for newest
                        out.append((prod,self._pkgList[prod][flav][-1],flav))

            return out

    def _listNewestProducts(self, product, flavor):
        prods = self.distServer.listAvailableProducts(product, None, flavor)
        names = {}
        flavors = {}
        out = []
        for p in prods:
            names[p[0]] = 1
            flavors[p[2]] = 1
        names = names.keys()
        names.sort()
        flavors = filter(lambda f: f != "generic", flavors.keys())
        flavors.sort()
        flavors.insert(0, "generic")
        
        for name in names:
            for flav in flavors:
                newest = filter(lambda p: p[0] == name and p[2] == flav, prods)
                newest.sort(lambda a,b: self.eups.version_cmp(a[1],b[1]))
                out.extend(newest)

        return out

    def getDistribFor(self, distId, options=None, flavor=None, tag=None):
        """
        return a Distrib instance for a given package distribution identifier.

        @param distId     the distribution identifier.  
        @param options    a set of options to pass to the Distrib instance
                           that fine-tunes its behavior.  (See DistribFactory)
        @param tag        a default tag name to associate with the package.
                           This is normally only relevent for creating 
                           packages for deployment on a server.  
        """
        if self.distServer is None:
            raise RuntimeError("No distribution server set")
        if options is not None and not isinstance(options, dict):
            raise ValueError("Repository.getDistribFor(): options not a " +
                             "dictionary")
        if not flavor:
            flavor = self.flavor

        opts = self._mergeOptions(options)

        return self.distFactory.createDistrib(distId, flavor, tag, opts,
                                              self.verbose, self.log)

    def isWritable(self):
        """
        return true if new packages, tag assignments, etc. can be added to 
        this repository.

        This implementation returns True only if the repository is accessible
        via local disk.
        """
        return LocalTransporter.canHandle(self.pkgroot)

    def create(self, serverRoot, distribTypeName, product, version, tag=None, 
               nodepend=False, options=None, manifest=None):
        """create and all necessary files for making a particular package
        available and deploy them into a local server directory.  This creates
        not only the requested product but also all of its dependencies unless
        nodepends is True.  Unless Eups.force is True, it will not recreate the 
        a package that is already deployed under the serverRoot directory.
        @param serverRoot   the root directory of a local server distribution
                              tree
        @param distribTypeName     the name of the distribution type to create.  The
                              recognized names are those registered to the 
                              DistribFactory passed to this Distribution's
                              constructor.  Names recognized by default include
                              "builder", "pacman", and "tarball".
        @param product      the name of the product to create
        @param version      the version of the product to create
        @param tag          if not None, update (or create) the tagged release
                              list for this tag name.  Only the information 
                              for the given product will be added/updated.  
                              (Default: True)
        @param nodepend     if True, only the requested product package will be 
                              created.  The product dependencies will be skipped.
                              (Default: False)
        @param manifest     an existing manifest filename; if provided, this 
                              will be used as the list of dependencies to assume
                              for this product, rather than generating the 
                              list from the EUPS database.  The deployed manifest
                              will be a modified version in that the distIDs for
                              undeployed packages will be updated and, if 
                              necessary, an entry will be added for the top
                              product (specified by the inputs to this function).
                              Note that this implementation will (unless 
                              nodepend is True) consult the remote server to 
                              determine if the package is available with the 
                              given distID.
        @param packageId     name:version for distribution; default product:version (either field may be omitted)
        """
        if not self.isWritable():
            raise RuntimeError("Unable to create packages for this repository (Choose a local repository)")

        opts = self._mergeOptions(options)

        try:
            distrib = \
                self.distFactory.createDistribByName(distribTypeName, 
                                                     options=opts, flavor=self.flavor,
                                                     verbosity=self.verbose)
        except KeyError:
            distrib = None
        if distrib is None:
            raise RuntimeError("%s: Distrib name not recognized" % distribTypeName)

        # load manifest data
        if manifest is None:
            # create it from what we (eups) know about it
            man = distrib.createDependencies(product, version, self.flavor)
        else:
            # load it from the given file
            man = Manifest.fromFile(manifest, self.eups, self.eups.verbose-1)

        # we will always overwrite the top package
        id = distrib.createPackage(serverRoot, product, version, self.flavor, overwrite=True)
        #
        # Any products in the top-level table file will be considered current for
        # the purposes of creating this distribution
        #
        for p in man.getProducts():
            self.eups.declareCurrent(p.product, p.version, local=True)

        if not nodepend:
            created = [ "%s-%s" % (product, version) ]
            self._recursiveCreate(serverRoot, distrib, man, created, True, distributionSet)

        # update the manifest record for the requested product
        dp = man.getDependency(product, version)
        if dp is None:
            # this product is not found in the manifest; this might happen if 
            # this function was passed a manifest file to use.  Just in case,
            # check the auto-generated manifest for a record and add it to
            # the given one
            tdp = distrib.createDependencies(product, version, self.flavor)
            tdp = template.getDependency(product, version)
            if template is not None:
               man.getProducts().append(tdp) 
        else:
            dp.distId = id

        # deploy the manifest file
        if packageId:
            vals = packageId.split(":")
            if len(vals) != 2:
                raise RuntimeError, ("Expected package Id of form name:version, saw \"%s\"" % packageId)
            if vals[0] == "":
                vals[0] = product
            if vals[1] == "":
                vals[1] = version
                
            packageName, packageVersion = vals
        else:
            packageName = product
            packageVersion = version

        distrib.writeManifest(serverRoot, man.getProducts(), packageName, packageVersion,
                              self.flavor, self.eups.force)
        
    def _recursiveCreate(self, serverRoot, distrib, manifest, created=None, 
                         recurse=True, distributionSet=None):
        if created is None: 
            created = []

        for dp in manifest.getProducts():
            pver = "%s-%s" % (dp.product, dp.version)
            if pver in created:
                continue

            # check to see if it looks like it is available in the format
            # given in the file
            if distrib.parseDistID(dp.distId) is None:
                # this may happen if create() was handed a manifest file
                if _availableAtLocation(dp):
                    continue
            else:
                flavor = dp.flavor
                if dp.flavor == "generic":   flavor = None
                if distrib.packageCreated(serverRoot, dp.product, dp.version, 
                                          flavor):
                    if not self.eups.force:
                        if self.verbose > 0:
                            print >> self.log, "Dependency %s %s already deployed; skipping" % (dp.product, dp.version)
                        created.append(pver)
                        continue

                    elif self.verbose > 0:
                        print >> self.log, "Overwriting existing dependency,", dp.product, dp.version
                        
            #
            # Check if this product is available elsewhere
            #
            if distributionSet and not self.eups.force:
                already_available = False # have we discovered that it's available from a server?
                for (pkgroot, pkgs) in distributionSet.pkgList:
                    if already_available:
                        break

                    for (p, v, f) in pkgs:
                        if (dp.product, dp.version) == (p, v):
                            if self.verbose > 0:
                                print >> self.log, "Dependency %s %s is already available from %s; skipping" % \
                                      (dp.product, dp.version, pkgroot)
                            already_available = True
                            break

                if already_available:
                    created.append(pver)
                    continue

            # we now should attempt to create this package because it appears 
            # not to be available
            man = distrib.createDependencies(dp.product, dp.version, self.flavor)

            id = distrib.createPackage(serverRoot, dp.product, dp.version, 
                                       self.flavor)
            created.append(pver)
            dp.distId = id
                
            if recurse:
                self._recursiveCreate(serverRoot, distrib, man, created, recurse, distributionSet)

            distrib.writeManifest(serverRoot, man.getProducts(), dp.product,
                                  dp.version, self.flavor, self.eups.force)

    def _availableAtLocation(self, dp, serverRoot):
        distrib = self.distFactory.createDistrib(dp.distId, dp.flavor, None,
                                                 self.options, self.verbose-2,
                                                 self.log)
        flavor = dp.flavor
        if flavor == generic:  flavor = None
        return distrib.packageCreated(serverRoot, dp.product, dp.version, flavor)

    def createTaggedRelease(self, serverRoot, tag, product, version=None, 
                            flavor=None, distrib=None):
        """create and release a named collection of products based on the 
        known dependencies of a given product.  
        @param serverRoot   the root directory of a local server distribution
                              tree
        @param tag          the name to give to this tagged release.  
        @param product      the name of the product to create
        @param version      the version of the product to create.  (Default:
                               the version marked current in the EUPS db)
        @param flavor       the flavor to associate with this release (Default:
                               "generic")
        @param distrib      a Distrib instance to use to determine which 
                              products to include.  If not provided, a default 
                              will be used.  
        """
        if distrib is not None and not isinstance(distrib, eupsDistrib.Distrib):
            raise TypeError("distrib parameter not a Distrib instance")
        validTags = self.getRecommendedTags(serverRoot)
        if not self.eups.force and tag not in validTags:
            raise RuntimeError("tag %s not amoung recommended tag names (%s)" %
                               (tag, ", ".join(validTags)))

        if not flavor:  flavor = "generic"

        if not version:
            version = self.eups.findCurrentVersion(product)[1]

        if distribTypeName:
            distrib = \
                self.distFactory.createDistribByName(distribTypeName, 
                                                     options=self.options,
                                                     verbosity=self.verbose,
                                                     log=self.log)
        else:
            distrib = DefaultDistrib(self.eups, self.distServer, self.flavor, 
                                     options=self.options, 
                                     verbosity=self.verbose,
                                     log=self.log)

        release = distrib.createTaggedRelease(serverRoot, tag, product, version,
                                              flavor)
        distrib.writeTaggedRelease(serverRoot, tag, products, flavor, 
                                   self.eups.flavor)

    def updateTaggedRelease(self, serverRoot, tag, product, version, 
                            flavor="generic", info=None, distrib=None):
        """update/add the version for a given product in a tagged release
        @param serverRoot   the root directory of a local server distribution
                              tree
        @param tag          the name to give to this tagged release.  
        @param product      the name of the product to create
        @param version      the version of the product to create.  (Default:
                               the version marked current in the EUPS db)
        @param flavor       the flavor to associate with this release (Default:
                               "generic")
        @param info         an optional list containing extra data to associate
                               with the product in the release.
        @param distrib      a Distrib instance to use to access tag release
                              information.  If not provided, a default will be 
                              used.  
        """
        if distrib is not None and not isinstance(distrib, eupsDistrib.Distrib):
            raise TypeError("distrib parameter not a Distrib instance")
        validTags = self.getRecommendedTags(serverRoot)
        if not self.eups.force and tag not in validTags:
            raise RuntimeError("tag %s not amoung recommended tag names (%s)" %
                               (tag, ", ".join(validTags)))

        if distrib is None:
            distrib = DefaultDistrib(self.eups, self.distServer, self.flavor, 
                                     options=self.options, 
                                     verbosity=self.verbose)

        pl = distrib.getTaggedRelease(serverRoot, tag, flavor)
        if pl is None:
            pl = TaggedProductList(tag, flavor)

        pl.addProduct(product, version, flavor)
        distrib.writeTaggedRelease(serverRoot, tag, pl, flavor, True);
            

    def clearServerCache(self):
        if self.distServer:
            self.distServer.clearConfigCache()

    